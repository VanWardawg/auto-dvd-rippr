import json
from pathlib import Path
from typing import Any

from .config import AppConfig
from .mapper import MappingError, analyze_dvd_menu, map_job_episodes
from .naming import NamingError, finalize_job_outputs
from .rip import RipError, execute_rip_job, recover_completed_rip
from .splitter import SplitError, execute_splits, plan_splits_for_job
from .state import InvalidTransitionError, append_job_log, get_job, transition_job
from .tmdb import TmdbError, identify_job_with_tmdb
from .transfer import TransferError, transfer_job_outputs


def resume_incomplete_jobs(conn, cfg: AppConfig, mock_rip: bool = False) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, status
        FROM jobs
        WHERE status IN ('queued', 'ripping', 'identifying', 'mapping', 'splitting', 'renaming', 'copying')
        ORDER BY created_at ASC
        """
    ).fetchall()
    results = []
    for row in rows:
        job_id = str(row["id"])
        try:
            result = run_pipeline_for_job(conn, cfg, job_id, mock_rip=mock_rip)
            results.append({"job_id": job_id, "result": result, "ok": True})
        except Exception as exc:
            results.append({"job_id": job_id, "ok": False, "error": str(exc)})
    return results


def run_pipeline_for_job(conn, cfg: AppConfig, job_id: str, mock_rip: bool = False) -> dict[str, Any]:
    job = get_job(conn, job_id)
    if not job:
        raise RuntimeError(f"Job not found: {job_id}")
    status = str(job["status"])

    try:
        if status == "queued":
            if str(job["media_type"] or "tv") == "tv":
                transition_job(conn, job_id, "identifying")
                status = "identifying"
            else:
                transition_job(conn, job_id, "ripping")
                status = _execute_rip_and_advance(conn, cfg, job_id, mock_rip=mock_rip)

        if status == "ripping":
            status = _execute_rip_and_advance(conn, cfg, job_id, mock_rip=mock_rip)

        if status == "identifying":
            movie_mode = str(job.get("movie_mode") or "single")
            required_movie_slots = _required_movie_slots(movie_mode)
            manual_selected = conn.execute(
                """
                SELECT tmdb_id, media_type
                FROM tmdb_candidates
                WHERE job_id = ? AND selected = 1 AND manual_override = 1
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            selected_movie_slots = conn.execute(
                """
                SELECT slot_index
                FROM job_selected_movies
                WHERE job_id = ?
                ORDER BY slot_index ASC
                """,
                (job_id,),
            ).fetchall()
            selected_media = conn.execute(
                """
                SELECT tmdb_id, media_type
                FROM job_selected_media
                WHERE job_id = ?
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            has_rip_titles = _job_has_rip_titles(conn, job_id)
            if str(job["media_type"] or "tv") == "movie" and movie_mode != "single" and len(selected_movie_slots) >= required_movie_slots:
                status = _advance_after_identify(conn, job_id, "movie", has_rip_titles=has_rip_titles)
            elif manual_selected and selected_media:
                status = _advance_after_identify(conn, job_id, str(selected_media["media_type"] or "tv"), has_rip_titles=has_rip_titles)
            else:
                ident = identify_job_with_tmdb(conn, cfg, job_id)
                if ident["needs_review"] and _should_retry_identify_with_menu_analysis(cfg, job_id, str(job["media_type"] or "tv")):
                    append_job_log(
                        conn,
                        job_id,
                        "INFO",
                        "TMDB identify needs review; attempting DVD menu analysis and retry before pausing.",
                        None,
                        None,
                    )
                    conn.commit()
                    try:
                        analyze_dvd_menu(conn, cfg, job_id)
                    except (MappingError, OSError) as exc:
                        append_job_log(
                            conn,
                            job_id,
                            "WARNING",
                            f"DVD menu analysis retry failed during identify fallback: {exc}",
                            None,
                            None,
                        )
                        conn.commit()
                    else:
                        ident = identify_job_with_tmdb(conn, cfg, job_id)
                if ident["needs_review"]:
                    return {"status": "identifying", "needs_review": True, "identify": ident}
                status = _advance_after_identify(
                    conn,
                    job_id,
                    str(ident["selected"]["media_type"] if ident.get("selected") else job["media_type"] or "tv"),
                    has_rip_titles=has_rip_titles,
                )

        if status == "ripping":
            status = _execute_rip_and_advance(conn, cfg, job_id, mock_rip=mock_rip)

        if status == "mapping":
            selected_media = conn.execute(
                """
                SELECT media_type
                FROM job_selected_media
                WHERE job_id = ?
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if selected_media and str(selected_media["media_type"] or "tv") == "movie":
                status = _advance_movie_past_mapping(conn, job_id)
            else:
                existing_mapping_state = _resume_existing_mappings_if_ready(conn, job_id)
                if existing_mapping_state is not None:
                    status = existing_mapping_state
                else:
                    mapped = map_job_episodes(conn, cfg, job_id)
                    if mapped["needs_review"]:
                        return {"status": "mapping", "needs_review": True, "mapping": mapped}
                    if any(m.get("needs_split") for m in mapped["mappings"]):
                        transition_job(conn, job_id, "splitting")
                        status = "splitting"
                    else:
                        transition_job(conn, job_id, "renaming")
                        status = "renaming"

        if status == "splitting":
            plan_splits_for_job(conn, job_id)
            execute_splits(conn, cfg, job_id)
            split_errors = conn.execute(
                "SELECT COUNT(*) AS c FROM split_plans WHERE job_id = ? AND status = 'error'",
                (job_id,),
            ).fetchone()["c"]
            if split_errors:
                return {"status": "splitting", "needs_review": True, "split_errors": int(split_errors)}
            transition_job(conn, job_id, "renaming")
            status = "renaming"

        if status == "renaming":
            finalize_job_outputs(conn, cfg, job_id)
            transition_job(conn, job_id, "copying")
            status = "copying"

        if status == "copying":
            transfer = transfer_job_outputs(conn, cfg, job_id)
            if transfer["errors"]:
                first_error = str(transfer["errors"][0].get("error") or "copy_failed")
                _transition_job_to_error_if_active(conn, job_id, f"NAS transfer failed: {first_error}")
                return {"status": "error", "needs_review": True, "transfer": transfer}
            transition_job(conn, job_id, "done")
            status = "done"

        return {"status": status}
    except (RipError, TmdbError, MappingError, SplitError, NamingError, TransferError) as exc:
        _transition_job_to_error_if_active(conn, job_id, str(exc))
        raise


def _should_retry_identify_with_menu_analysis(cfg: AppConfig, job_id: str, media_type: str) -> bool:
    if media_type != "movie":
        return False

    analysis_path = Path(cfg.staging_root) / "jobs" / job_id / "menu_analysis" / "menu_analysis.json"
    if not analysis_path.exists():
        return True

    try:
        payload = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True

    if not isinstance(payload, dict):
        return True

    return "media_title_hints" not in payload


def _advance_after_identify(conn, job_id: str, media_type: str, *, has_rip_titles: bool) -> str:
    next_status = "renaming" if media_type == "movie" else ("mapping" if has_rip_titles else "ripping")
    if media_type == "movie":
        append_job_log(
            conn,
            job_id,
            "INFO",
            "Movie job identified; skipping TV episode mapping and moving to finalization.",
            None,
            None,
        )
        conn.commit()
    elif not has_rip_titles:
        append_job_log(
            conn,
            job_id,
            "INFO",
            "TV job identified before rip; starting rip with selected TMDB season context.",
            None,
            None,
        )
        conn.commit()
    transition_job(conn, job_id, next_status)
    return next_status


def _advance_movie_past_mapping(conn, job_id: str) -> str:
    append_job_log(
        conn,
        job_id,
        "INFO",
        "Movie job was left in mapping; skipping directly to finalization.",
        None,
        None,
    )
    conn.commit()
    transition_job(conn, job_id, "renaming")
    return "renaming"


def _required_movie_slots(movie_mode: str | None) -> int:
    if movie_mode == "double_feature":
        return 2
    if movie_mode == "trilogy":
        return 3
    return 1


def _job_has_rip_titles(conn, job_id: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM rip_titles WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return bool(row and int(row["c"] or 0) > 0)


def _execute_rip_and_advance(conn, cfg: AppConfig, job_id: str, *, mock_rip: bool) -> str:
    recovered = recover_completed_rip(conn, cfg, job_id)
    if recovered is None:
        job = get_job(conn, job_id)
        execute_rip_job(
            conn,
            cfg,
            job_id,
            optical_drive=(job or {}).get("optical_drive"),
            mock=mock_rip,
        )
    return _advance_after_rip(conn, job_id)


def _advance_after_rip(conn, job_id: str) -> str:
    selected_media = conn.execute(
        """
        SELECT media_type
        FROM job_selected_media
        WHERE job_id = ?
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    next_status = "mapping" if selected_media and str(selected_media["media_type"] or "tv") == "tv" else "identifying"
    transition_job(conn, job_id, next_status)
    return next_status


def _transition_job_to_error_if_active(conn, job_id: str, message: str) -> None:
    current = get_job(conn, job_id)
    if not current:
        return
    current_status = str(current.get("status") or "")
    if current_status == "error":
        return
    try:
        transition_job(conn, job_id, "error", message)
    except InvalidTransitionError:
        conn.execute(
            "UPDATE jobs SET status = 'error', current_stage = 'error', updated_at = datetime('now'), error_message = ? WHERE id = ?",
            (message, job_id),
        )
        append_job_log(
            conn,
            job_id,
            "ERROR",
            f"Forced transition {current_status} -> error",
            current_status,
            "error",
        )
        conn.commit()


def _resume_existing_mappings_if_ready(conn, job_id: str) -> str | None:
    rows = conn.execute(
        """
        SELECT id, episode_start, confidence, manual_override, needs_split
        FROM episode_mappings
        WHERE job_id = ?
        ORDER BY id ASC
        """,
        (job_id,),
    ).fetchall()
    if not rows:
        return None

    mapped_rows = [row for row in rows if row["episode_start"] is not None]
    if not mapped_rows:
        return None

    unresolved = [
        row for row in mapped_rows
        if float(row["confidence"] or 0.0) < 0.85 and int(row["manual_override"] or 0) != 1
    ]
    if unresolved:
        return None

    if any(int(row["needs_split"] or 0) == 1 for row in mapped_rows):
        transition_job(conn, job_id, "splitting")
        return "splitting"

    transition_job(conn, job_id, "renaming")
    return "renaming"

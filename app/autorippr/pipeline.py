import json
import threading
from pathlib import Path
from typing import Any

from .config import AppConfig
from .mapper import MappingError, analyze_dvd_menu, map_job_episodes
from .job_ops import purge_local_files
from .logger import get_logger
from .naming import NamingError, finalize_job_outputs
from .rip import RipError, eject_drive, execute_rip_job, recover_completed_rip
from .splitter import SplitError, execute_splits, plan_splits_for_job
from .state import InvalidTransitionError, append_job_log, get_job, set_awaiting_review, transition_job
from .tmdb import TmdbError, identify_job_with_tmdb
from .transfer import TransferError, ensure_nas_available, transfer_job_outputs

log = get_logger("pipeline")


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


def _reclaim_local_space(conn, cfg: AppConfig, job_id: str) -> None:
    """
    Drop the staged rip once the output is confirmed on the NAS.

    Staging space is the binding constraint on a machine working through a
    collection: every disc leaves several gigabytes behind, and the files are
    redundant the moment the NAS copy is verified. Off by default, because
    deleting the only local copy should be a deliberate choice.
    """
    if not cfg.clear_local_after_transfer:
        return
    try:
        result = purge_local_files(cfg.staging_root, job_id)
    except OSError as exc:
        append_job_log(
            conn,
            job_id,
            "WARNING",
            f"Could not reclaim staged files: {exc}",
            None,
            None,
        )
        conn.commit()
        return

    freed = int(result.get("freed_bytes") or 0)
    if not freed:
        return
    append_job_log(
        conn,
        job_id,
        "INFO",
        f"Reclaimed {freed / (1024 ** 3):.2f} GB of staging space; the NAS copy is verified.",
        None,
        None,
    )
    conn.commit()


def release_disc(conn, cfg: AppConfig, job_id: str, *, reason: str) -> bool:
    """
    Eject the disc once nothing else needs it.

    Ejecting is what makes an unattended session work: the drive is otherwise
    held for as long as the job takes, and a job paused for review holds it
    indefinitely -- 35 minutes at the median, and much longer overnight. With
    the tray open, swapping in the next disc is enough to start the next job,
    with no need to touch the UI at all.

    The disc is *not* finished with when the rip ends: DVD menu analysis reads
    VIDEO_TS straight off the drive. This is called only once that has run or
    been ruled out.
    """
    if not cfg.eject_after_rip:
        return False

    job = get_job(conn, job_id) or {}
    drive = job.get("optical_drive")
    ejected = eject_drive(drive)
    append_job_log(
        conn,
        job_id,
        "INFO" if ejected else "WARNING",
        (
            f"Ejected {drive or 'optical drive'} ({reason}); ready for the next disc."
            if ejected
            else f"Could not eject {drive or 'optical drive'} ({reason})."
        ),
        None,
        None,
    )
    conn.commit()
    return ejected


def _capture_menu_artifacts_before_release(conn, cfg: AppConfig, job_id: str) -> None:
    """
    Cache whatever the DVD menu can tell us while the disc is still readable.

    Episode mapping reads these artifacts from staging rather than the disc, so
    capturing them here is what allows the drive to be released before mapping
    runs. Best effort: a disc with no usable menu is normal, and must not stop
    the pipeline.
    """
    try:
        analyze_dvd_menu(conn, cfg, job_id)
    except (MappingError, OSError) as exc:
        append_job_log(
            conn,
            job_id,
            "WARNING",
            f"Could not cache DVD menu artifacts before ejecting: {exc}",
            None,
            None,
        )
        conn.commit()


def _resume_errored_job(conn, job) -> str:
    """Move an errored job back to the stage it should retry from."""
    job_id = str(job["id"])
    stage = _infer_resume_stage(conn, job)
    append_job_log(
        conn,
        job_id,
        "INFO",
        f"Retrying failed job from the '{stage}' stage.",
        None,
        None,
    )
    conn.commit()
    transition_job(conn, job_id, stage)
    return stage


def _infer_resume_stage(conn, job) -> str:
    """
    Decide where a failed job should pick up, based on what it already produced.

    Work already done is expensive -- a rip is tens of minutes and gigabytes --
    so the rule is to resume at the earliest stage whose output is missing,
    never to redo a stage whose artifacts are on record.
    """
    job_id = str(job["id"])

    # Finalized files exist, so the rip, identification and naming all
    # succeeded. Whatever failed, the remaining work is the copy.
    outputs = conn.execute(
        "SELECT COUNT(*) AS c FROM outputs WHERE job_id = ?", (job_id,)
    ).fetchone()["c"]
    if outputs:
        # ...unless the film was re-identified after they were built. Correcting
        # a misidentification on a finalized job otherwise changed nothing the
        # user could see: Resume went straight to the copy and sent the file
        # under its old, wrong name. Re-finalizing is cheap -- it renames local
        # files -- and it is the only way the correction reaches the NAS.
        if _selection_is_newer_than_outputs(conn, job_id):
            return "renaming"
        return "copying"

    if not _job_has_rip_titles(conn, job_id):
        # Nothing was ripped; there is no shortcut to take.
        return "queued"

    selected = conn.execute(
        "SELECT media_type FROM job_selected_media WHERE job_id = ? LIMIT 1",
        (job_id,),
    ).fetchone()
    if not selected:
        return "identifying"

    media_type = str(selected["media_type"] or job["media_type"] or "tv")
    if media_type == "movie":
        return "renaming"

    mappings = conn.execute(
        "SELECT COUNT(*) AS c FROM episode_mappings WHERE job_id = ?", (job_id,)
    ).fetchone()["c"]
    if not mappings:
        return "mapping"

    needs_split = conn.execute(
        "SELECT COUNT(*) AS c FROM episode_mappings WHERE job_id = ? AND needs_split = 1",
        (job_id,),
    ).fetchone()["c"]
    if needs_split:
        unfinished_splits = conn.execute(
            "SELECT COUNT(*) AS c FROM split_plans WHERE job_id = ? AND status != 'done'",
            (job_id,),
        ).fetchone()["c"]
        no_plans = conn.execute(
            "SELECT COUNT(*) AS c FROM split_plans WHERE job_id = ?", (job_id,)
        ).fetchone()["c"] == 0
        if unfinished_splits or no_plans:
            return "splitting"

    return "renaming"


def run_pipeline_for_job(conn, cfg: AppConfig, job_id: str, mock_rip: bool = False) -> dict[str, Any]:
    job = get_job(conn, job_id)
    if not job:
        raise RuntimeError(f"Job not found: {job_id}")
    status = str(job["status"])

    if status == "error":
        # Without this, resuming an errored job matched no stage below and
        # returned silently, so the UI's Resume button did nothing at all.
        status = _resume_errored_job(conn, job)
        job = get_job(conn, job_id) or job

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
                if has_rip_titles:
                    release_disc(conn, cfg, job_id, reason="identified")
            elif manual_selected and selected_media:
                status = _advance_after_identify(conn, job_id, str(selected_media["media_type"] or "tv"), has_rip_titles=has_rip_titles)
                if has_rip_titles:
                    release_disc(conn, cfg, job_id, reason="identified")
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

                    # Menu analysis runs for minutes -- a median of one, and
                    # eleven on one recorded job -- and the user can resolve
                    # the job by hand the whole time it does. One really did:
                    # the job reached `done` at 03:42 and this attempt woke at
                    # 03:58 to mark it awaiting review and eject the drive,
                    # which by then could have held a different disc.
                    if not _job_is_still_identifying(conn, job_id):
                        current = str((get_job(conn, job_id) or {}).get("status") or "unknown")
                        append_job_log(
                            conn,
                            job_id,
                            "INFO",
                            f"Job was resolved elsewhere while menu analysis ran (now {current}); "
                            "abandoning this identify attempt.",
                            None,
                            None,
                        )
                        conn.commit()
                        return {"status": current, "superseded": True}

                if ident["needs_review"]:
                    # Identification has done all it can; the disc is no longer
                    # needed, and this job is about to sit waiting for a human.
                    # Free the drive so the next disc can go in.
                    if has_rip_titles:
                        release_disc(conn, cfg, job_id, reason="waiting for review")
                    set_awaiting_review(conn, job_id, True)
                    return {"status": "identifying", "needs_review": True, "identify": ident}
                status = _advance_after_identify(
                    conn,
                    job_id,
                    str(ident["selected"]["media_type"] if ident.get("selected") else job["media_type"] or "tv"),
                    has_rip_titles=has_rip_titles,
                )
                if has_rip_titles:
                    release_disc(conn, cfg, job_id, reason="identified")

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
                elif _job_has_manual_mappings(conn, job_id):
                    # The user has assigned episodes by hand. Re-running the
                    # mapper here would delete that work and replace it with
                    # guesses -- which it did, repeatedly, until this guard.
                    # If readiness still fails with manual rows present,
                    # something needs a human, not a remap.
                    append_job_log(
                        conn,
                        job_id,
                        "WARNING",
                        "Not re-mapping over manual episode assignments; "
                        "waiting for review instead.",
                        None,
                        None,
                    )
                    set_awaiting_review(conn, job_id, True)
                    return {"status": "mapping", "needs_review": True}
                else:
                    mapped = map_job_episodes(conn, cfg, job_id)
                    if mapped["needs_review"]:
                        set_awaiting_review(conn, job_id, True)
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
                set_awaiting_review(conn, job_id, True)
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
            _reclaim_local_space(conn, cfg, job_id)

        return {"status": status}
    except (RipError, TmdbError, MappingError, SplitError, NamingError, TransferError) as exc:
        _transition_job_to_error_if_active(conn, job_id, str(exc))
        raise
    except BaseException as exc:
        # Anything not on the domain list -- a locked database, an OS error, a
        # bug -- used to propagate straight past here, killing the process with
        # the job still sitting in its active status. That left a zombie: the
        # UI showed it as ripping forever, the MakeMKV child kept running with
        # nobody reading it, and Resume was unavailable because Resume only
        # applies to errored jobs. Marking it errored is what makes the work
        # recoverable instead of lost.
        _mark_error_best_effort(conn, job_id, f"{type(exc).__name__}: {exc}")
        raise


def _mark_error_best_effort(conn, job_id: str, message: str) -> None:
    """
    Record the error without ever raising a second exception.

    The most likely reason for landing here is that the database is
    unavailable -- which is also the reason writing the error state might
    fail. A failure here must not replace the original exception, because the
    original is the one worth seeing in the log.
    """
    try:
        _transition_job_to_error_if_active(conn, job_id, message)
    except BaseException:  # noqa: BLE001 - deliberately swallowing
        log.exception("could not record job error state", extra={"job_id": job_id})


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


# How long the pre-rip courtesy check may take. Probing a disconnected SMB
# share blocks on a network timeout that can run to tens of seconds, and
# ripping is entirely local -- it must never wait on the NAS.
NAS_PREFLIGHT_TIMEOUT_SECONDS = 2.0


def _warn_if_nas_unreachable(conn, cfg: AppConfig, job_id: str) -> None:
    """
    Mention up front if the destination is missing. Never block the rip.

    Ripping does not need the NAS, so this is advisory only: it warns, and the
    rip proceeds regardless. The share may well come back before the copy
    stage, and if it does not, the job stops at 'copying' with a clear message
    and resumes from there once the user reconnects -- no re-rip.

    The probe runs on a daemon thread with a short deadline so a dead share
    cannot stall the start of a rip. An inconclusive probe says nothing rather
    than guessing.
    """
    result: list[str] = []

    def probe() -> None:
        try:
            ensure_nas_available(cfg)
        except TransferError as exc:
            result.append(str(exc))
        except Exception:  # never let a probe failure touch the rip
            pass

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(NAS_PREFLIGHT_TIMEOUT_SECONDS)

    if worker.is_alive() or not result:
        # Timed out, or the NAS is fine. Either way, get on with the rip.
        return

    append_job_log(
        conn,
        job_id,
        "WARNING",
        (
            "NAS is not reachable right now. The rip will continue -- it is local -- "
            f"but the copy will stop until the share returns. {result[0]}"
        ),
        None,
        None,
    )
    conn.commit()




def _selection_is_newer_than_outputs(conn, job_id: str) -> bool:
    """
    Whether the film was re-identified after this job's outputs were finalized.

    Compares when the user last chose a title against when the finalized
    manifest was written. Nothing records which TMDB id an output was named
    for, but the timestamps answer the same question: a selection made after
    finalization means the files on disk carry the previous film's name.
    """
    selected = conn.execute(
        "SELECT updated_at FROM job_selected_media WHERE job_id = ? LIMIT 1", (job_id,)
    ).fetchone()
    manifest = conn.execute(
        "SELECT created_at FROM finalized_manifests WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if not selected or not manifest:
        return False
    chosen = str(selected["updated_at"] or "")
    built = str(manifest["created_at"] or "")
    if not chosen or not built:
        return False
    return chosen > built


def _job_is_still_identifying(conn, job_id: str) -> bool:
    """
    Whether this job is still where the identify attempt left it.

    Mirrors `_job_is_still_ripping` in rip.py: any work that runs for minutes
    has to re-check the job before acting, because the job can be finished,
    cancelled, or deleted underneath it.
    """
    job = get_job(conn, job_id)
    return bool(job) and str(job.get("status") or "") == "identifying"


def _execute_rip_and_advance(conn, cfg: AppConfig, job_id: str, *, mock_rip: bool) -> str:
    recovered = recover_completed_rip(conn, cfg, job_id)
    if recovered is None:
        _warn_if_nas_unreachable(conn, cfg, job_id)
        job = get_job(conn, job_id)
        execute_rip_job(
            conn,
            cfg,
            job_id,
            optical_drive=(job or {}).get("optical_drive"),
            mock=mock_rip,
        )
        if not mock_rip and cfg.eject_after_rip:
            job = get_job(conn, job_id) or {}
            if str(job.get("media_type") or "tv") == "tv":
                # TV identifies before ripping, so the only remaining use for
                # the disc is menu analysis. Cache it now, then let go.
                _capture_menu_artifacts_before_release(conn, cfg, job_id)
                release_disc(conn, cfg, job_id, reason="rip complete")
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


def _job_has_manual_mappings(conn, job_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM episode_mappings WHERE job_id = ? AND manual_override = 1 LIMIT 1",
        (job_id,),
    ).fetchone()
    return row is not None


def _resume_existing_mappings_if_ready(conn, job_id: str) -> str | None:
    rows = conn.execute(
        """
        SELECT id, rip_title_id, episode_start, confidence, manual_override, needs_split
        FROM episode_mappings
        WHERE job_id = ?
        ORDER BY id ASC
        """,
        (job_id,),
    ).fetchall()
    if not rows:
        return None

    # Rows without a rip title assign nothing -- they are the "episodes X have
    # no mapped title" report. One of them held a job hostage: it sat at 0.20
    # with no way to resolve it (the guided review only shows rows tied to
    # ripped files), so every save re-mapped, wiped the user's overrides, and
    # produced a fresh unresolvable row. Three times, on one disc.
    mapped_rows = [
        row for row in rows
        if row["episode_start"] is not None and row["rip_title_id"] is not None
    ]
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

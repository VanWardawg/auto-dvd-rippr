import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .state import append_job_log


class NamingError(RuntimeError):
    pass


INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*]')


def select_likely_movie_feature_rows(rip_rows) -> list[Any]:
    if not rip_rows:
        return []

    rows_with_duration = [
        row for row in rip_rows
        if row["duration_seconds"] is not None and float(row["duration_seconds"]) > 0
    ]
    if not rows_with_duration:
        return list(rip_rows)

    feature_rows = [
        row for row in rows_with_duration
        if float(row["duration_seconds"]) >= 45 * 60
    ]
    if feature_rows:
        return feature_rows

    sorted_rows = sorted(
        rows_with_duration,
        key=lambda row: float(row["duration_seconds"] or 0.0),
        reverse=True,
    )
    longest = float(sorted_rows[0]["duration_seconds"] or 0.0)
    second_longest = float(sorted_rows[1]["duration_seconds"] or 0.0) if len(sorted_rows) > 1 else 0.0

    # Allow short features when one title clearly dominates the rest,
    # while still blocking collections of similarly short extras.
    dominance_floor = max(longest * 0.90, second_longest * 1.40 if second_longest > 0 else 0.0, 30 * 60)
    dominant_rows = [
        row for row in sorted_rows
        if float(row["duration_seconds"] or 0.0) >= dominance_floor
    ]
    return dominant_rows


def finalize_job_outputs(conn, cfg: AppConfig, job_id: str) -> dict[str, Any]:
    job = conn.execute(
        """
        SELECT media_type, movie_mode
        FROM jobs
        WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    if not job:
        raise NamingError("Job not found for finalization.")

    sel = conn.execute(
        """
        SELECT media_type, tmdb_id, title, year, season_number
        FROM job_selected_media
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if not sel:
        if str(job["media_type"] or "tv") == "movie" and str(job["movie_mode"] or "single") != "single":
            sel = None
        else:
            raise NamingError("No selected media metadata found for naming.")

    media_type = str(job["media_type"] or (sel["media_type"] if sel else "tv"))
    movie_mode = str(job["movie_mode"] or "single")
    base_title = _sanitize_name(str(sel["title"])) if sel else ""
    year = sel["year"] if sel else None
    title_year = f"{base_title} ({year})" if year else base_title

    finalize_root = Path(cfg.staging_root) / "jobs" / job_id / "finalized"
    finalize_root.mkdir(parents=True, exist_ok=True)

    conn.execute("DELETE FROM outputs WHERE job_id = ?", (job_id,))

    if media_type == "movie" and movie_mode != "single":
        manifest_items = _finalize_multi_movie(conn, cfg, job_id, finalize_root)
    elif media_type == "movie":
        source = _pick_movie_source(conn, job_id)
        if not source:
            raise NamingError("No source file found for movie finalization.")
        target_dir = finalize_root / title_year
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{title_year}.mkv"
        written, status, reason = _place_file(source, target_path, cfg.collision_policy)
        out_row = _insert_output(conn, job_id, written, status=status, last_error=reason)
        manifest_items = [{
            "output_id": out_row,
            "source_file": str(source),
            "local_path": str(written),
            "status": status,
            "reason": reason,
        }]
    else:
        manifest_items = _finalize_tv(conn, cfg, job_id, finalize_root, title_year, sel["season_number"])

    manifest = {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "items": manifest_items,
    }
    conn.execute(
        "INSERT INTO finalized_manifests (job_id, manifest_json, created_at) VALUES (?, ?, ?)",
        (job_id, json.dumps(manifest, ensure_ascii=True), manifest["created_at"]),
    )
    append_job_log(conn, job_id, "INFO", f"Finalization complete. Items={len(manifest_items)}", None, None)
    conn.commit()
    return manifest


def _finalize_multi_movie(conn, cfg: AppConfig, job_id: str, finalize_root: Path) -> list[dict[str, Any]]:
    slots = conn.execute(
        """
        SELECT slot_index, tmdb_id, title, year, rip_title_id
        FROM job_selected_movies
        WHERE job_id = ?
        ORDER BY slot_index ASC
        """,
        (job_id,),
    ).fetchall()
    if not slots:
        raise NamingError("No selected movie slots found for movie-pack finalization.")

    assigned = _assign_movie_slots(conn, job_id, slots)
    items: list[dict[str, Any]] = []
    for slot in assigned:
        source = _pick_movie_source_by_rip_title(conn, int(slot["rip_title_id"]))
        if not source:
            raise NamingError(f"No source file found for movie slot {int(slot['slot_index'])}.")
        base_title = _sanitize_name(str(slot["title"]))
        year = slot["year"]
        title_year = f"{base_title} ({year})" if year else base_title
        target_dir = finalize_root / title_year
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{title_year}.mkv"
        written, status, reason = _place_file(source, target_path, cfg.collision_policy)
        out_row = _insert_output(conn, job_id, written, status=status, last_error=reason)
        items.append(
            {
                "output_id": out_row,
                "slot_index": int(slot["slot_index"]),
                "tmdb_id": int(slot["tmdb_id"]),
                "source_file": str(source),
                "local_path": str(written),
                "status": status,
                "reason": reason,
            }
        )
    return items


def _finalize_tv(conn, cfg: AppConfig, job_id: str, finalize_root: Path, title_year: str, season_number) -> list[dict[str, Any]]:
    season_no = int(season_number or 1)
    season_dir = finalize_root / title_year / f"Season {season_no:02d}"
    season_dir.mkdir(parents=True, exist_ok=True)

    mappings = conn.execute(
        """
        SELECT em.id, em.episode_start, em.episode_end, em.tmdb_episode_ids_json, em.episode_titles_json, em.needs_split,
               rt.source_file
        FROM episode_mappings em
        LEFT JOIN rip_titles rt ON rt.id = em.rip_title_id
        WHERE em.job_id = ? AND em.episode_start IS NOT NULL
        ORDER BY em.episode_start ASC
        """,
        (job_id,),
    ).fetchall()

    items: list[dict[str, Any]] = []
    for m in mappings:
        eps = list(range(int(m["episode_start"]), int(m["episode_end"]) + 1))
        titles_json = json.loads(m["episode_titles_json"] or "[]")
        ep_titles = [
            str(titles_json[i]) if i < len(titles_json) and titles_json[i] else f"Episode {ep}"
            for i, ep in enumerate(eps)
        ]
        split_rows = conn.execute(
            """
            SELECT output_file, segment_index
            FROM split_plans
            WHERE mapping_id = ? AND status = 'done'
            ORDER BY segment_index ASC
            """,
            (int(m["id"]),),
        ).fetchall()

        if split_rows:
            for idx, split_row in enumerate(split_rows):
                source = Path(str(split_row["output_file"]))
                if not source.exists():
                    continue
                ep_num = eps[idx] if idx < len(eps) else eps[-1]
                ep_title = ep_titles[idx] if idx < len(ep_titles) else ep_titles[-1]
                episode_token = f"s{season_no:02d}e{ep_num:02d}"
                filename = f"{title_year} - {episode_token} - {_sanitize_name(ep_title)}.mkv"
                target_path = season_dir / filename
                written, status, reason = _place_file(source, target_path, cfg.collision_policy)
                out_row = _insert_output(conn, job_id, written, status=status, last_error=reason)
                items.append(
                    {
                        "output_id": out_row,
                        "mapping_id": int(m["id"]),
                        "source_file": str(source),
                        "local_path": str(written),
                        "status": status,
                        "reason": reason,
                    }
                )
        else:
            source = _pick_source_for_mapping(conn, int(m["id"]), str(m["source_file"] or ""))
            if not source:
                continue
            episode_token = f"s{season_no:02d}e{eps[0]:02d}"
            if len(eps) > 1:
                episode_token = f"s{season_no:02d}e{eps[0]:02d}-e{eps[-1]:02d}"
            title_suffix = " & ".join(_sanitize_name(t) for t in ep_titles if t) or f"Episode {eps[0]}"
            filename = f"{title_year} - {episode_token} - {title_suffix}.mkv"
            target_path = season_dir / filename
            written, status, reason = _place_file(source, target_path, cfg.collision_policy)
            out_row = _insert_output(conn, job_id, written, status=status, last_error=reason)
            items.append(
                {
                    "output_id": out_row,
                    "mapping_id": int(m["id"]),
                    "source_file": str(source),
                    "local_path": str(written),
                    "status": status,
                    "reason": reason,
                }
            )
    return items


def _pick_source_for_mapping(conn, mapping_id: int, default_source: str) -> Path | None:
    split = conn.execute(
        """
        SELECT output_file
        FROM split_plans
        WHERE mapping_id = ? AND status = 'done'
        ORDER BY segment_index ASC
        LIMIT 1
        """,
        (mapping_id,),
    ).fetchone()
    if split and split["output_file"]:
        p = Path(str(split["output_file"]))
        if p.exists():
            return p
    if default_source:
        p = Path(default_source)
        if p.exists():
            return p
    return None


def _pick_movie_source(conn, job_id: str) -> Path | None:
    split_row = conn.execute(
        """
        SELECT output_file AS p
        FROM split_plans
        WHERE job_id = ? AND status = 'done'
        ORDER BY end_seconds - start_seconds DESC, id ASC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if split_row and split_row["p"]:
        path = Path(str(split_row["p"]))
        if path.exists():
            return path

    rip_rows = conn.execute(
        """
        SELECT source_file, duration_seconds
        FROM rip_titles
        WHERE job_id = ?
        ORDER BY duration_seconds DESC, title_id ASC, id ASC
        """,
        (job_id,),
    ).fetchall()
    if not rip_rows:
        return None

    main_rows = select_likely_movie_feature_rows(rip_rows)
    if not main_rows and len(rip_rows) > 1:
        return None
    candidate_rows = main_rows or rip_rows
    for row in candidate_rows:
        path = Path(str(row["source_file"]))
        if path.exists():
            return path
    return None


def _pick_movie_source_by_rip_title(conn, rip_title_id: int) -> Path | None:
    row = conn.execute(
        """
        SELECT source_file AS p
        FROM rip_titles
        WHERE id = ?
        """,
        (rip_title_id,),
    ).fetchone()
    if not row:
        return None
    path = Path(str(row["p"]))
    return path if path.exists() else None


def _assign_movie_slots(conn, job_id: str, slots) -> list[dict[str, Any]]:
    rip_rows = conn.execute(
        """
        SELECT id, title_id, duration_seconds
        FROM rip_titles
        WHERE job_id = ?
        ORDER BY title_id ASC, id ASC
        """,
        (job_id,),
    ).fetchall()
    if not rip_rows:
        raise NamingError("No rip titles found for movie-pack finalization.")

    main_rows = select_likely_movie_feature_rows(rip_rows)
    source_rows = main_rows if len(main_rows) >= len(slots) else rip_rows
    if len(source_rows) < len(slots):
        raise NamingError("Not enough rip titles found to assign selected movie slots.")

    assigned: list[dict[str, Any]] = []
    for slot, rip_row in zip(slots, source_rows, strict=False):
        rip_title_id = int(slot["rip_title_id"]) if slot["rip_title_id"] is not None else int(rip_row["id"])
        conn.execute(
            """
            UPDATE job_selected_movies
            SET rip_title_id = ?, updated_at = ?
            WHERE job_id = ? AND slot_index = ?
            """,
            (rip_title_id, datetime.now(timezone.utc).isoformat(), job_id, int(slot["slot_index"])),
        )
        assigned.append(
            {
                **dict(slot),
                "rip_title_id": rip_title_id,
            }
        )
    conn.commit()
    return assigned


def _insert_output(conn, job_id: str, local_path: Path, status: str, last_error: str | None) -> int:
    cur = conn.execute(
        """
        INSERT INTO outputs (job_id, local_path, transfer_status, last_error)
        VALUES (?, ?, ?, ?)
        """,
        (job_id, str(local_path), "pending" if status == "ok" else "error", last_error),
    )
    return int(cur.lastrowid)


def _place_file(source: Path, target: Path, collision_policy: str) -> tuple[Path, str, str | None]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if collision_policy == "overwrite":
            target.unlink()
        else:
            return target, "skipped", "collision_skip"
    shutil.copy2(source, target)
    return target, "ok", None


def _sanitize_name(value: str) -> str:
    v = INVALID_PATH_CHARS.sub("", value).strip()
    v = re.sub(r"\s+", " ", v)
    return v[:180] if len(v) > 180 else v

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import AppConfig
from .state import append_job_log


class JobDeleteError(RuntimeError):
    pass


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_has_column(conn, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(r["name"]) == column for r in cols)


# Child tables deleted in this order first, where they exist. Order matters
# where one child references another; anything not listed here is discovered
# and appended automatically.
_JOB_CHILD_TABLE_ORDER = (
    "finalized_manifests",
    "outputs",
    "split_plans",
    "episode_mappings",
    "tmdb_candidates",
    "rip_titles",
    "job_logs",
    "job_selected_media",
    "job_selected_movies",
    "job_progress",
)


def _tables_referencing_jobs(conn) -> list[tuple[str, str]]:
    """
    Every (table, column) with a foreign key to jobs, in deletion order.

    Discovered from the schema rather than hardcoded. A hardcoded list silently
    goes stale the moment someone adds a table -- and because foreign keys are
    enforced, the resulting failure is a bare "FOREIGN KEY constraint failed"
    on the final DELETE, with no hint about which table was missed. That is
    exactly how deleting a job broke when job_progress was added.
    """
    discovered: dict[str, str] = {}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        table = str(row["name"])
        if table == "jobs":
            continue
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            if str(fk["table"]).lower() == "jobs":
                discovered[table] = str(fk["from"])
                break

    ordered = [(t, discovered[t]) for t in _JOB_CHILD_TABLE_ORDER if t in discovered]
    listed = {t for t, _ in ordered}
    ordered.extend(sorted((t, c) for t, c in discovered.items() if t not in listed))
    return ordered


# Staging directories that hold the large intermediate files. A rip is several
# gigabytes and everything here is reproducible from the disc, so this is the
# space that gets reclaimed once a job's output is safely on the NAS.
LOCAL_ARTIFACT_DIRS = (
    "rip_output",
    "split_output",
    "finalized",
    "menu_analysis",
    "dvdnav_menu",
    "dvd_arch_menu",
    "ocr",
)


def local_artifact_bytes(staging_root: str, job_id: str) -> int:
    """How much disk a job's staged files are currently occupying."""
    job_dir = Path(staging_root) / "jobs" / job_id
    total = 0
    for name in LOCAL_ARTIFACT_DIRS:
        total += _directory_bytes(job_dir / name)
    return total


def _directory_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _remove_local_artifact_dirs(staging_root: str, job_id: str) -> tuple[list[str], int]:
    """Delete the staged directories, reporting what went and how much it freed."""
    job_dir = Path(staging_root) / "jobs" / job_id
    removed: list[str] = []
    freed = 0
    for name in LOCAL_ARTIFACT_DIRS:
        path = job_dir / name
        if not path.exists():
            continue
        freed += _directory_bytes(path)
        shutil.rmtree(path, ignore_errors=True)
        removed.append(str(path))
    return removed, freed


def purge_local_files(staging_root: str, job_id: str) -> dict[str, Any]:
    """
    Delete a job's staged files but keep its database records.

    This is the automatic counterpart to clear_job_local_artifacts. The manual
    action also drops the outputs/rip_titles rows, which is fine when a person
    deliberately reclaims space, but doing that automatically on every job
    would discard the record of where each file landed on the NAS and its
    checksum -- the provenance worth keeping long after the bytes are gone.
    """
    removed, freed = _remove_local_artifact_dirs(staging_root, job_id)
    return {"job_id": job_id, "removed_paths": removed, "freed_bytes": freed}


def summarize_reclaimable(conn, staging_root: str) -> dict[str, Any]:
    """
    What could be freed right now, without ripping anything again.

    Only finished jobs count. A job still in flight needs its staged files, and
    an errored one may be one Resume away from finishing -- clearing those
    would turn a retry into a re-rip.
    """
    rows = conn.execute(
        "SELECT id, disc_label FROM jobs WHERE status = 'done' ORDER BY updated_at ASC"
    ).fetchall()
    jobs: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        job_id = str(row["id"])
        try:
            size = local_artifact_bytes(staging_root, job_id)
        except OSError:
            continue
        if size <= 0:
            continue
        total += size
        jobs.append({"job_id": job_id, "disc_label": str(row["disc_label"] or ""), "bytes": size})
    return {"total_bytes": total, "job_count": len(jobs), "jobs": jobs}


def reclaim_completed_jobs(conn, staging_root: str) -> dict[str, Any]:
    """
    Free the staged files of every completed job in one go.

    Clearing 170 jobs one at a time is the same problem at a different scale,
    and it is the scale at which the disk actually fills up. Files only: the
    NAS path and checksum of each output stay on record.
    """
    summary = summarize_reclaimable(conn, staging_root)
    freed = 0
    cleared: list[str] = []
    for entry in summary["jobs"]:
        result = purge_local_files(staging_root, entry["job_id"])
        freed += int(result.get("freed_bytes") or 0)
        if result.get("removed_paths"):
            cleared.append(entry["job_id"])
    return {"freed_bytes": freed, "job_count": len(cleared), "job_ids": cleared}


def delete_job(conn, staging_root: str, job_id: str) -> dict[str, Any]:
    exists = conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not exists:
        raise JobDeleteError(f"Job not found: {job_id}")

    out_rows = []
    if _table_has_column(conn, "outputs", "job_id"):
        out_rows = conn.execute("SELECT id FROM outputs WHERE job_id = ?", (job_id,)).fetchall()
    output_ids = [int(r["id"]) for r in out_rows]
    if _table_has_column(conn, "transfer_attempts", "output_id"):
        for out_id in output_ids:
            conn.execute("DELETE FROM transfer_attempts WHERE output_id = ?", (out_id,))

    deleted_counts: dict[str, int] = {}
    for table, column in _tables_referencing_jobs(conn):
        cur = conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (job_id,))
        deleted_counts[table] = int(cur.rowcount if cur.rowcount is not None else 0)

    # jobs table uses primary key column "id", not "job_id".
    cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    deleted_counts["jobs"] = int(cur.rowcount if cur.rowcount is not None else 0)

    conn.commit()

    job_dir = Path(staging_root) / "jobs" / job_id
    removed_job_dir = False
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
        removed_job_dir = True

    return {
        "job_id": job_id,
        "removed_job_dir": removed_job_dir,
        "deleted_counts": deleted_counts,
        "deleted_transfer_attempts": len(output_ids),
    }


def clear_job_local_artifacts(conn, staging_root: str, job_id: str) -> dict[str, Any]:
    """
    Free a job's staged files, keeping its record of what it produced.

    This used to delete the outputs, rip_titles, episode_mappings and
    split_plans rows as well, which left a finished job reporting "0 titles
    ripped, 0 on NAS" even though its file was sitting on the NAS. The bytes
    are reproducible from the disc; the record of where each output landed and
    its checksum is not, and it costs nothing to keep.
    """
    exists = conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not exists:
        raise JobDeleteError(f"Job not found: {job_id}")

    removed, freed_bytes = _remove_local_artifact_dirs(staging_root, job_id)

    # The local paths no longer exist, so stop claiming they do.
    db_cleanup_warning = None
    try:
        if _table_has_column(conn, "outputs", "local_path"):
            conn.execute(
                "UPDATE outputs SET local_path = '' WHERE job_id = ? AND nas_path IS NOT NULL",
                (job_id,),
            )
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        db_cleanup_warning = str(exc)

    return {
        "job_id": job_id,
        "removed_paths": removed,
        "freed_bytes": freed_bytes,
        "db_cleanup_warning": db_cleanup_warning,
    }


def clear_job_output_artifacts(conn, job_id: str) -> dict[str, Any]:
    exists = conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not exists:
        raise JobDeleteError(f"Job not found: {job_id}")

    out_rows = []
    if _table_has_column(conn, "outputs", "job_id"):
        out_rows = conn.execute(
            "SELECT id, local_path, nas_path FROM outputs WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    output_ids = [int(r["id"]) for r in out_rows]

    removed_files: list[str] = []
    for row in out_rows:
        for column in ("local_path", "nas_path"):
            path_value = row[column]
            if not path_value:
                continue
            path = Path(str(path_value))
            if path.exists() and path.is_file():
                try:
                    path.unlink()
                    removed_files.append(str(path))
                except OSError:
                    pass

    if _table_has_column(conn, "transfer_attempts", "output_id"):
        for out_id in output_ids:
            conn.execute("DELETE FROM transfer_attempts WHERE output_id = ?", (out_id,))

    if _table_has_column(conn, "outputs", "job_id"):
        conn.execute("DELETE FROM outputs WHERE job_id = ?", (job_id,))
    if _table_has_column(conn, "finalized_manifests", "job_id"):
        conn.execute("DELETE FROM finalized_manifests WHERE job_id = ?", (job_id,))
    conn.commit()

    return {
        "job_id": job_id,
        "removed_output_files": removed_files,
        "deleted_transfer_attempts": len(output_ids),
    }


def remap_job_remote_output(conn, cfg: AppConfig, job_id: str) -> dict[str, Any]:
    job = conn.execute(
        """
        SELECT id, media_type, movie_mode
        FROM jobs
        WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    if not job:
        raise JobDeleteError(f"Job not found: {job_id}")

    if str(job["media_type"] or "movie") != "movie" or str(job["movie_mode"] or "single") != "single":
        raise JobDeleteError("Remote remap currently supports single-movie jobs only.")

    selected_media = conn.execute(
        """
        SELECT title, year
        FROM job_selected_media
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if not selected_media:
        raise JobDeleteError("No selected movie metadata found for remote remap.")

    dest_path = _build_movie_remote_path(cfg.nas_root, str(selected_media["title"] or ""), selected_media["year"])
    if dest_path.exists():
        append_job_log(conn, job_id, "INFO", f"Remote remap skipped; output already at {dest_path}", None, None)
        conn.commit()
        return {
            "job_id": job_id,
            "status": "already_correct",
            "destination_path": str(dest_path),
        }

    source_candidates = _find_remote_movie_candidates(conn, cfg.nas_root, job_id, dest_path)
    if not source_candidates:
        raise JobDeleteError("No remote movie output found at candidate paths for this job.")
    if len(source_candidates) > 1:
        raise JobDeleteError(
            "Multiple remote movie outputs matched candidate paths: "
            + "; ".join(str(path) for path in source_candidates)
        )

    source_path = source_candidates[0]
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_path), str(dest_path))
    _remove_empty_parent_dir(source_path.parent)

    if _table_has_column(conn, "outputs", "job_id") and _table_has_column(conn, "outputs", "nas_path"):
        conn.execute(
            """
            UPDATE outputs
            SET nas_path = ?, transfer_status = 'done', last_error = NULL
            WHERE job_id = ?
            """,
            (str(dest_path), job_id),
        )
    append_job_log(conn, job_id, "INFO", f"Remote output remapped: {source_path} -> {dest_path}", None, None)
    conn.commit()
    return {
        "job_id": job_id,
        "status": "moved",
        "source_path": str(source_path),
        "destination_path": str(dest_path),
    }


def cancel_job(conn, job_id: str) -> dict[str, Any]:
    exists = conn.execute("SELECT id, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not exists:
        raise JobDeleteError(f"Job not found: {job_id}")

    stopped_pids = _stop_job_processes(job_id)
    conn.execute(
        """
        UPDATE jobs
        SET status = 'error', current_stage = 'error', error_message = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        ("Cancelled by user.", job_id),
    )
    conn.execute(
        """
        INSERT INTO job_logs (job_id, timestamp, level, message, from_status, to_status)
        VALUES (?, datetime('now'), 'WARNING', ?, ?, 'error')
        """,
        (job_id, f"Job cancelled by user. Stopped PIDs: {stopped_pids}", str(exists["status"])),
    )
    conn.commit()
    return {"job_id": job_id, "stopped_pids": stopped_pids}


def _find_remote_movie_candidates(conn, nas_root: str, job_id: str, dest_path: Path) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    if _table_has_column(conn, "outputs", "job_id") and _table_has_column(conn, "outputs", "nas_path"):
        output_rows = conn.execute(
            """
            SELECT nas_path
            FROM outputs
            WHERE job_id = ?
            ORDER BY id ASC
            """,
            (job_id,),
        ).fetchall()
        for row in output_rows:
            nas_path = row["nas_path"]
            if not nas_path:
                continue
            path = Path(str(nas_path))
            if path.exists() and path != dest_path and str(path) not in seen:
                seen.add(str(path))
                candidates.append(path)

    tmdb_rows = conn.execute(
        """
        SELECT title, year
        FROM tmdb_candidates
        WHERE job_id = ?
        ORDER BY selected DESC, score DESC, id ASC
        """,
        (job_id,),
    ).fetchall()
    for row in tmdb_rows:
        title = str(row["title"] or "").strip()
        if not title:
            continue
        path = _build_movie_remote_path(nas_root, title, row["year"])
        if path.exists() and path != dest_path and str(path) not in seen:
            seen.add(str(path))
            candidates.append(path)
    return candidates


def _build_movie_remote_path(nas_root: str, title: str, year: Any) -> Path:
    safe_title = _sanitize_name(title)
    title_year = f"{safe_title} ({year})" if year else safe_title
    return Path(nas_root) / "Movies" / title_year / f"{title_year}.mkv"


def _sanitize_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "", str(value or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:180] if len(cleaned) > 180 else cleaned


def _remove_empty_parent_dir(path: Path) -> None:
    try:
        if path.exists() and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        return


def _stop_job_processes(job_id: str) -> list[int]:
    current_pid = os.getpid()
    script = (
        "$procs = Get-CimInstance Win32_Process | "
        f"Where-Object {{$_.CommandLine -like '*{job_id}*' -and $_.ProcessId -ne {current_pid} -and $_.Name -ne 'powershell.exe'}} | "
        "Select-Object ProcessId | ConvertTo-Json -Compress"
    )
    probe = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    payload = (probe.stdout or "").strip()
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []
    rows = parsed if isinstance(parsed, list) else [parsed]
    pids = [int(row["ProcessId"]) for row in rows if isinstance(row, dict) and row.get("ProcessId") is not None]
    for pid in pids:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    return pids

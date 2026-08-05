import subprocess
from pathlib import Path
from typing import Any

from .config import AppConfig
from .state import append_job_log


class SplitError(RuntimeError):
    pass


def plan_splits_for_job(conn, job_id: str) -> dict[str, Any]:
    mappings = conn.execute(
        """
        SELECT em.id, em.rip_title_id, em.episode_start, em.episode_end, em.tmdb_episode_ids_json,
               rt.source_file, rt.chapter_count, rt.duration_seconds
        FROM episode_mappings em
        LEFT JOIN rip_titles rt ON em.rip_title_id = rt.id
        WHERE em.job_id = ? AND em.needs_split = 1
        ORDER BY em.id ASC
        """,
        (job_id,),
    ).fetchall()
    conn.execute("DELETE FROM split_plans WHERE job_id = ?", (job_id,))
    planned = []
    for m in mappings:
        source = m["source_file"]
        if not source:
            continue
        start_ep = int(m["episode_start"])
        end_ep = int(m["episode_end"])
        count = max(1, end_ep - start_ep + 1)
        chapters = int(m["chapter_count"] or 0)
        duration_seconds = float(m["duration_seconds"] or 0.0)
        chapter_bounds = _extract_chapter_bounds(conn, int(m["rip_title_id"]) if m["rip_title_id"] else None)
        for idx in range(count):
            chapter_start = None
            chapter_end = None
            start_seconds = None
            end_seconds = None
            if chapters >= count and chapters > 0:
                per = max(1, chapters // count)
                chapter_start = idx * per + 1
                chapter_end = chapters if idx == count - 1 else (idx + 1) * per
                if chapter_bounds:
                    start_idx = max(1, chapter_start) - 1
                    end_idx = max(1, chapter_end) - 1
                    if start_idx < len(chapter_bounds):
                        start_seconds = chapter_bounds[start_idx][0]
                    if end_idx < len(chapter_bounds):
                        end_seconds = chapter_bounds[end_idx][1]
            # Fallback when chapter timing is unavailable: even split by duration
            if (start_seconds is None or end_seconds is None) and duration_seconds > 0 and count > 1:
                seg = duration_seconds / count
                start_seconds = round(idx * seg, 3)
                end_seconds = round((idx + 1) * seg if idx < count - 1 else duration_seconds, 3)
            conn.execute(
                """
                INSERT INTO split_plans (
                    job_id, mapping_id, source_file, segment_index,
                    start_seconds, end_seconds, chapter_start, chapter_end, output_file, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (job_id, int(m["id"]), source, idx + 1, start_seconds, end_seconds, chapter_start, chapter_end, None),
            )
            planned.append(
                {
                    "mapping_id": int(m["id"]),
                    "segment_index": idx + 1,
                    "source_file": source,
                    "chapter_start": chapter_start,
                    "chapter_end": chapter_end,
                    "start_seconds": start_seconds,
                    "end_seconds": end_seconds,
                }
            )
    append_job_log(conn, job_id, "INFO", f"Split plans generated: {len(planned)} segment(s).", None, None)
    conn.commit()
    return {"job_id": job_id, "split_plan_count": len(planned), "plans": planned}


def set_manual_split_timestamps(
    conn,
    split_plan_id: int,
    start_seconds: float | None,
    end_seconds: float | None,
) -> dict[str, Any]:
    row = conn.execute("SELECT id, job_id FROM split_plans WHERE id = ?", (split_plan_id,)).fetchone()
    if not row:
        raise SplitError(f"Split plan not found: {split_plan_id}")
    conn.execute(
        """
        UPDATE split_plans
        SET start_seconds = ?, end_seconds = ?, chapter_start = NULL, chapter_end = NULL, status = 'pending'
        WHERE id = ?
        """,
        (start_seconds, end_seconds, split_plan_id),
    )
    append_job_log(
        conn,
        row["job_id"],
        "INFO",
        f"Manual split timestamps set on split_plan_id={split_plan_id}: {start_seconds}-{end_seconds}",
        None,
        None,
    )
    conn.commit()
    return {"split_plan_id": split_plan_id, "start_seconds": start_seconds, "end_seconds": end_seconds}


def execute_splits(conn, cfg: AppConfig, job_id: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, source_file, segment_index, start_seconds, end_seconds, chapter_start, chapter_end
        FROM split_plans
        WHERE job_id = ?
        ORDER BY id ASC
        """,
        (job_id,),
    ).fetchall()
    if not rows:
        return {"job_id": job_id, "executed": 0, "outputs": []}

    out_dir = Path(cfg.staging_root) / "jobs" / job_id / "split_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []

    for row in rows:
        plan_id = int(row["id"])
        source_file = Path(str(row["source_file"]))
        if not source_file.exists():
            _mark_plan_error(conn, plan_id, f"Source file not found: {source_file}")
            continue

        output_file = out_dir / f"{source_file.stem}.part{int(row['segment_index']):02d}.mkv"
        cmd = _build_ffmpeg_split_cmd(
            ffmpeg_path=cfg.ffmpeg_path,
            source_file=source_file,
            output_file=output_file,
            start_seconds=row["start_seconds"],
            end_seconds=row["end_seconds"],
            chapter_start=row["chapter_start"],
            chapter_end=row["chapter_end"],
        )

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _mark_plan_error(conn, plan_id, str(exc))
            continue

        if proc.returncode != 0:
            _mark_plan_error(conn, plan_id, f"ffmpeg failed (exit={proc.returncode}): {proc.stderr[:500]}")
            continue

        if not output_file.exists():
            _mark_plan_error(conn, plan_id, "ffmpeg exited successfully but output file is missing.")
            continue

        if not _validate_split_duration(output_file, cfg):
            _mark_plan_error(conn, plan_id, "Split output duration outside configured sanity bounds.")
            if output_file.exists():
                output_file.unlink()
            continue

        conn.execute(
            "UPDATE split_plans SET output_file = ?, status = 'done' WHERE id = ?",
            (str(output_file), plan_id),
        )
        outputs.append(str(output_file))

    conn.commit()
    append_job_log(conn, job_id, "INFO", f"Split execution completed. Outputs: {len(outputs)}", None, None)
    conn.commit()
    return {"job_id": job_id, "executed": len(rows), "outputs": outputs}


def _build_ffmpeg_split_cmd(
    ffmpeg_path: str,
    source_file: Path,
    output_file: Path,
    start_seconds,
    end_seconds,
    chapter_start,
    chapter_end,
) -> list[str]:
    ffmpeg = Path(ffmpeg_path)
    if not ffmpeg.exists():
        raise SplitError(f"ffmpeg not found: {ffmpeg_path}")

    cmd = [str(ffmpeg), "-y", "-i", str(source_file)]
    if start_seconds is not None:
        cmd.extend(["-ss", str(start_seconds)])
    if end_seconds is not None:
        cmd.extend(["-to", str(end_seconds)])
    if start_seconds is None and end_seconds is None:
        raise SplitError(
            "Split segment lacks timing boundaries. Set manual timestamps in GUI/CLI."
        )
    cmd.extend(["-c", "copy", str(output_file)])
    return cmd


def _mark_plan_error(conn, split_plan_id: int, message: str) -> None:
    conn.execute(
        "UPDATE split_plans SET status = 'error', error_message = ? WHERE id = ?",
        (message, split_plan_id),
    )


def _extract_chapter_bounds(conn, rip_title_id: int | None) -> list[tuple[float, float]]:
    if not rip_title_id:
        return []
    row = conn.execute(
        "SELECT raw_metadata_json FROM rip_titles WHERE id = ?",
        (rip_title_id,),
    ).fetchone()
    if not row or not row["raw_metadata_json"]:
        return []
    import json

    try:
        payload = json.loads(row["raw_metadata_json"])
    except json.JSONDecodeError:
        return []
    raw = payload.get("ffprobe_raw", payload)
    chapters = raw.get("chapters") if isinstance(raw, dict) else None
    if not isinstance(chapters, list):
        return []
    out: list[tuple[float, float]] = []
    for ch in chapters:
        try:
            st = float(ch.get("start_time"))
            en = float(ch.get("end_time"))
            out.append((st, en))
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _validate_split_duration(path: Path, cfg: AppConfig) -> bool:
    ffprobe = Path(cfg.ffprobe_path)
    if not ffprobe.exists():
        return True
    cmd = [
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    import json

    try:
        payload = json.loads(proc.stdout or "{}")
        dur = float(payload.get("format", {}).get("duration"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    minutes = dur / 60.0
    return cfg.min_episode_minutes <= minutes <= cfg.max_episode_minutes

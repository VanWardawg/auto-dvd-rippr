import json
import platform
import re
import subprocess
import csv
import time
from datetime import datetime, timezone
from io import StringIO
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .logger import get_logger
from .state import append_job_log, get_job


log = get_logger("rip")


class RipError(RuntimeError):
    pass


@dataclass(frozen=True)
class RipTitleMetadata:
    title_id: int
    duration_seconds: float | None
    chapter_count: int | None
    source_file: str
    raw_metadata: dict[str, Any]


def discover_optical_drives() -> list[dict[str, Any]]:
    if platform.system().lower() != "windows":
        return []

    import ctypes  # Windows-specific runtime call

    kernel32 = ctypes.windll.kernel32
    drives_bitmask = kernel32.GetLogicalDrives()
    DRIVE_CDROM = 5

    results: list[dict[str, Any]] = []
    for idx in range(26):
        if not (drives_bitmask & (1 << idx)):
            continue

        letter = f"{chr(65 + idx)}:"
        root = f"{letter}\\"
        drive_type = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        if drive_type != DRIVE_CDROM:
            continue

        volume_name = ctypes.create_unicode_buffer(261)
        fs_name = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_ulong()
        max_component = ctypes.c_ulong()
        flags = ctypes.c_ulong()

        has_media = bool(
            kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(root),
                volume_name,
                260,
                ctypes.byref(serial),
                ctypes.byref(max_component),
                ctypes.byref(flags),
                fs_name,
                260,
            )
        )

        results.append(
            {
                "drive": letter,
                "root": root,
                "has_media": has_media,
                "volume_label": volume_name.value if has_media else "",
            }
        )
    return results


def execute_rip_job(
    conn,
    cfg: AppConfig,
    job_id: str,
    disc_index: int = 0,
    mock: bool = False,
) -> dict[str, Any]:
    staging_root = Path(cfg.staging_root)
    job_root = staging_root / "jobs" / job_id
    rip_output_dir = job_root / "rip_output"
    log_dir = job_root / "logs"
    rip_output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    makemkv_log_path = log_dir / "makemkv.log"

    log_already_written = False
    if mock:
        mkv_files = _run_mock_rip(rip_output_dir)
        rip_log_text = "MOCK RIP MODE\nCreated fake MKV files for validation.\n"
        disc_info_by_title: dict[int, dict[str, Any]] = {}
    else:
        append_job_log(
            conn=conn,
            job_id=job_id,
            level="INFO",
            message="Starting MakeMKV disc-info scan.",
            from_status=None,
            to_status=None,
        )
        conn.commit()
        disc_info_text, disc_info_by_title = _read_makemkv_disc_info(
            makemkv_path=cfg.makemkv_path,
            disc_index=disc_index,
            timeout_seconds=min(cfg.rip_timeout_seconds, 45),
        )
        if disc_info_text:
            (log_dir / "makemkv_disc_info.log").write_text(disc_info_text, encoding="utf-8")
            append_job_log(
                conn=conn,
                job_id=job_id,
                level="INFO",
                message="Disc-info scan completed.",
                from_status=None,
                to_status=None,
            )
        else:
            append_job_log(
                conn=conn,
                job_id=job_id,
                level="WARNING",
                message="Disc-info scan unavailable/timed out; continuing with rip.",
                from_status=None,
                to_status=None,
            )
        conn.commit()
        _ensure_job_still_ripping(conn, job_id)
        append_job_log(
            conn=conn,
            job_id=job_id,
            level="INFO",
            message=f"Starting MakeMKV rip to {rip_output_dir}",
            from_status=None,
            to_status=None,
        )
        conn.commit()
        rip_log_text, exit_code = _run_makemkv_rip_streaming(
            conn=conn,
            job_id=job_id,
            makemkv_path=cfg.makemkv_path,
            output_dir=rip_output_dir,
            log_path=makemkv_log_path,
            disc_index=disc_index,
            timeout_seconds=cfg.rip_timeout_seconds,
        )
        log_already_written = True
        if exit_code != 0:
            raise RipError(
                "MakeMKV failed with non-zero exit code. "
                f"See log: {makemkv_log_path}"
            )
        mkv_files = sorted(rip_output_dir.glob("*.mkv"))

    if not mkv_files:
        raise RipError("Rip completed but no MKV files were found in staging output.")

    if not log_already_written:
        makemkv_log_path.write_text(rip_log_text, encoding="utf-8")
    append_job_log(
        conn=conn,
        job_id=job_id,
        level="INFO",
        message=f"MakeMKV log saved: {makemkv_log_path}",
        from_status=None,
        to_status=None,
    )

    titles = _build_title_metadata(
        ffprobe_path=cfg.ffprobe_path,
        mkv_files=mkv_files,
        mock=mock,
        disc_info_by_title=disc_info_by_title,
    )
    _persist_rip_titles(conn, job_id, titles)
    conn.commit()

    append_job_log(
        conn=conn,
        job_id=job_id,
        level="INFO",
        message=f"Ripped {len(titles)} title(s) to {rip_output_dir}",
        from_status=None,
        to_status=None,
    )
    conn.commit()

    return {
        "job_id": job_id,
        "mock": mock,
        "rip_output_dir": str(rip_output_dir),
        "makemkv_log_path": str(makemkv_log_path),
        "titles_ripped": len(titles),
        "titles": [
            {
                "title_id": t.title_id,
                "duration_seconds": t.duration_seconds,
                "chapter_count": t.chapter_count,
                "source_file": t.source_file,
            }
            for t in titles
        ],
    }


def recover_completed_rip(conn, cfg: AppConfig, job_id: str) -> dict[str, Any] | None:
    job_root = Path(cfg.staging_root) / "jobs" / job_id
    rip_output_dir = job_root / "rip_output"
    log_dir = job_root / "logs"
    makemkv_log_path = log_dir / "makemkv.log"
    if not rip_output_dir.exists() or not makemkv_log_path.exists():
        return None

    log_text = makemkv_log_path.read_text(encoding="utf-8", errors="replace")
    if "Copy complete." not in log_text and "titles saved" not in log_text:
        return None

    mkv_files = sorted(rip_output_dir.glob("*.mkv"))
    if not mkv_files:
        return None

    disc_info_path = log_dir / "makemkv_disc_info.log"
    disc_info_by_title: dict[int, dict[str, Any]] = {}
    if disc_info_path.exists():
        try:
            disc_info_by_title = _parse_makemkv_info_output(disc_info_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            disc_info_by_title = {}

    titles = _build_title_metadata(
        ffprobe_path=cfg.ffprobe_path,
        mkv_files=mkv_files,
        mock=False,
        disc_info_by_title=disc_info_by_title,
    )
    if not titles:
        return None
    _persist_rip_titles(conn, job_id, titles)
    append_job_log(
        conn=conn,
        job_id=job_id,
        level="WARNING",
        message=f"Recovered completed rip from existing output. Titles={len(titles)}",
        from_status=None,
        to_status=None,
    )
    conn.commit()
    return {
        "job_id": job_id,
        "rip_output_dir": str(rip_output_dir),
        "makemkv_log_path": str(makemkv_log_path),
        "titles_ripped": len(titles),
        "titles": [
            {
                "title_id": t.title_id,
                "duration_seconds": t.duration_seconds,
                "chapter_count": t.chapter_count,
                "source_file": t.source_file,
            }
            for t in titles
        ],
    }


def _run_makemkv_rip_streaming(
    conn,
    job_id: str,
    makemkv_path: str,
    output_dir: Path,
    log_path: Path,
    disc_index: int,
    timeout_seconds: int,
) -> tuple[str, int]:
    makemkv = _resolve_makemkv_cli_path(makemkv_path)
    if not makemkv.exists():
        raise RipError(f"MakeMKV executable not found: {makemkv_path}")

    cmd = [
        str(makemkv),
        "-r",
        "--progress=-same",
        "mkv",
        f"disc:{disc_index}",
        "all",
        str(output_dir),
    ]
    log.info("running makemkv", extra={"command": " ".join(cmd)})
    start = time.monotonic()
    next_heartbeat = start + 15.0
    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"COMMAND: {' '.join(cmd)}\n\n")
            lf.flush()
            proc = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
            )
            while True:
                now = time.monotonic()
                if not _job_is_still_ripping(conn, job_id):
                    proc.kill()
                    raise RipError(f"Rip cancelled or no longer active for job {job_id}.")
                if now >= next_heartbeat:
                    _emit_rip_heartbeat(conn, job_id, output_dir)
                    next_heartbeat = now + 15.0
                if proc.poll() is not None:
                    break
                if now - start > timeout_seconds:
                    proc.kill()
                    raise RipError(f"MakeMKV timed out after {timeout_seconds}s.")
                time.sleep(1.0)

            exit_code = int(proc.returncode or 0)
            lf.write(f"\nEXIT_CODE: {exit_code}\n")
            lf.flush()
    except OSError as exc:
        raise RipError(f"Failed to execute MakeMKV: {exc}") from exc

    return "", exit_code


def _emit_rip_heartbeat(conn, job_id: str, output_dir: Path) -> None:
    files = list(output_dir.glob("*"))
    count = len(files)
    total_bytes = 0
    latest_mtime = None
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        total_bytes += int(st.st_size)
        latest_mtime = st.st_mtime if latest_mtime is None else max(latest_mtime, st.st_mtime)
    mb = round(total_bytes / (1024 * 1024), 1)
    msg = f"Rip heartbeat: files={count}, size_mb={mb}"
    append_job_log(conn, job_id, "INFO", msg, None, None)
    conn.execute(
        "UPDATE jobs SET updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), job_id),
    )
    conn.commit()


def _ensure_job_still_ripping(conn, job_id: str) -> None:
    if not _job_is_still_ripping(conn, job_id):
        raise RipError(f"Rip cancelled or no longer active for job {job_id}.")


def _job_is_still_ripping(conn, job_id: str) -> bool:
    job = get_job(conn, job_id)
    if not job:
        return False
    return str(job.get("status")) == "ripping"


def _read_makemkv_disc_info(
    makemkv_path: str,
    disc_index: int,
    timeout_seconds: int,
) -> tuple[str, dict[int, dict[str, Any]]]:
    makemkv = _resolve_makemkv_cli_path(makemkv_path)
    if not makemkv.exists():
        return "", {}
    cmd = [str(makemkv), "-r", "info", f"disc:{disc_index}"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "", {}
    output = (
        f"COMMAND: {' '.join(cmd)}\n"
        f"EXIT_CODE: {proc.returncode}\n\n"
        f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}\n"
    )
    parsed = _parse_makemkv_info_output(proc.stdout or "")
    return output, parsed


def _resolve_makemkv_cli_path(makemkv_path: str) -> Path:
    """
    Resolve to a CLI-capable MakeMKV binary.
    Users often configure makemkv.exe (GUI), but ripping requires makemkvcon(.exe).
    """
    configured = Path(makemkv_path)
    if configured.exists():
        name = configured.name.lower()
        if name.startswith("makemkvcon"):
            return configured
        parent = configured.parent
        for candidate in ("makemkvcon64.exe", "makemkvcon.exe"):
            cpath = parent / candidate
            if cpath.exists():
                return cpath
        return configured

    # Last-resort sibling search from the configured parent even if configured file is missing.
    parent = configured.parent
    for candidate in ("makemkvcon64.exe", "makemkvcon.exe", "makemkv.exe"):
        cpath = parent / candidate
        if cpath.exists():
            return cpath
    return configured


def _run_mock_rip(rip_output_dir: Path) -> list[Path]:
    mock_files: list[Path] = []
    for index in (1, 2):
        path = rip_output_dir / f"title_t{index:02d}.mkv"
        path.write_bytes(b"mock-mkv-placeholder")
        mock_files.append(path)
    return mock_files


def _build_title_metadata(
    ffprobe_path: str,
    mkv_files: list[Path],
    mock: bool,
    disc_info_by_title: dict[int, dict[str, Any]],
) -> list[RipTitleMetadata]:
    rows: list[RipTitleMetadata] = []
    for idx, mkv_file in enumerate(sorted(mkv_files), start=1):
        title_id = _extract_title_id(mkv_file.stem, idx)
        if mock:
            duration_seconds = 1200.0 + (idx * 60.0)
            chapter_count = 8
            raw = {"source": "mock", "index": idx}
        else:
            probe = _ffprobe_file(ffprobe_path, mkv_file)
            duration_seconds = probe.get("duration_seconds")
            chapter_count = probe.get("chapter_count")
            raw = probe
            if title_id in disc_info_by_title:
                raw["makemkv_info"] = disc_info_by_title[title_id]
                raw["menu_name"] = disc_info_by_title[title_id].get("display_name")

        rows.append(
            RipTitleMetadata(
                title_id=title_id,
                duration_seconds=duration_seconds,
                chapter_count=chapter_count,
                source_file=str(mkv_file),
                raw_metadata=raw,
            )
        )
    return rows


def _extract_title_id(stem: str, fallback: int) -> int:
    match = re.search(r"(?i)(?:title|t)[_\- ]?(\d{1,3})", stem)
    if match:
        return int(match.group(1))
    return fallback


def _ffprobe_file(ffprobe_path: str, file_path: Path) -> dict[str, Any]:
    ffprobe = Path(ffprobe_path)
    if not ffprobe.exists():
        return {"error": f"ffprobe not found: {ffprobe_path}"}

    cmd = [
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_chapters",
        str(file_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}

    if proc.returncode != 0:
        return {
            "error": "ffprobe_failed",
            "exit_code": proc.returncode,
            "stderr": proc.stderr.strip(),
        }

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": "ffprobe_json_parse_failed"}

    duration_val = None
    fmt = payload.get("format")
    if isinstance(fmt, dict):
        dur = fmt.get("duration")
        if dur is not None:
            try:
                duration_val = float(dur)
            except (TypeError, ValueError):
                duration_val = None

    chapters = payload.get("chapters")
    chapter_count = len(chapters) if isinstance(chapters, list) else None

    return {
        "duration_seconds": duration_val,
        "chapter_count": chapter_count,
        "ffprobe_raw": payload,
    }


def _persist_rip_titles(conn, job_id: str, titles: list[RipTitleMetadata]) -> None:
    conn.execute("DELETE FROM rip_titles WHERE job_id = ?", (job_id,))
    for title in titles:
        conn.execute(
            """
            INSERT INTO rip_titles (
                job_id,
                title_id,
                duration_seconds,
                chapter_count,
                source_file,
                raw_metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                title.title_id,
                title.duration_seconds,
                title.chapter_count,
                title.source_file,
                json.dumps(title.raw_metadata, ensure_ascii=True),
            ),
        )


def _parse_makemkv_info_output(stdout: str) -> dict[int, dict[str, Any]]:
    """
    Parse MakeMKV `info` output and extract per-title metadata, including a best-effort display name.
    """
    per_title: dict[int, dict[str, Any]] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("TINFO:"):
            continue
        # Parse CSV-like line robustly (quoted values, commas inside quotes).
        try:
            row = next(csv.reader(StringIO(line)))
        except Exception:
            continue
        if len(row) < 4:
            continue
        try:
            title_id = int(row[0].split(":", 1)[1])
            code = int(row[1])
        except (IndexError, ValueError):
            continue
        value = row[3].strip() if len(row) > 3 else ""
        entry = per_title.setdefault(title_id, {"fields": {}})
        entry["fields"][str(code)] = value

    # Best-effort human label extraction from known/common fields
    for title_id, entry in per_title.items():
        fields = entry.get("fields", {})
        candidates = [
            fields.get("27"),  # often filename-like
            fields.get("26"),  # language/name variant
            fields.get("16"),  # stream description-ish in some discs
            fields.get("2"),   # generic text field fallback
        ]
        display = next((c for c in candidates if isinstance(c, str) and c.strip()), None)
        if display:
            entry["display_name"] = display.strip()
    return per_title

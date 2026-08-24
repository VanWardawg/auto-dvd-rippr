import calendar
import csv
import json
import platform
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from io import StringIO
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .makemkv import (
    PROGRESS_MAX,
    build_title_candidates,
    overall_fraction,
    parse_progress_line,
    select_titles,
)
from .progress import clear_progress, upsert_progress
from .logger import get_logger
from .state import append_job_log, get_job


log = get_logger("rip")

MAKEMKV_BETA_KEY_URL = "https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053"


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


def eject_drive(optical_drive: str | None) -> bool:
    """
    Open the drive tray so the next disc can go straight in.

    Uses the Windows MCI interface via ctypes rather than shelling out, so no
    console window flashes and no extra dependency is needed. Returns False
    rather than raising: failing to eject must never fail a completed rip.
    """
    if platform.system().lower() != "windows":
        return False

    try:
        import ctypes  # Windows-specific runtime call

        alias = "autorippr_eject"
        letter = (optical_drive or "").strip().rstrip("\/")
        if letter:
            open_cmd = f"open {letter} type cdaudio alias {alias}"
        else:
            open_cmd = f"open cdaudio alias {alias}"

        mci = ctypes.windll.winmm.mciSendStringW
        if mci(open_cmd, None, 0, None) != 0:
            return False
        try:
            return mci(f"set {alias} door open", None, 0, None) == 0
        finally:
            mci(f"close {alias}", None, 0, None)
    except Exception:
        return False


def get_makemkv_status(makemkv_path: str) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    resolved = _resolve_makemkv_cli_path(makemkv_path)
    if not makemkv_path.strip():
        return {
            "level": "ok",
            "message": "",
            "details": [],
            "build_version": None,
            "can_rip": None,
            "beta_key_expires_at": None,
            "days_until_expiry": None,
            "checked_at": checked_at,
            "source_url": MAKEMKV_BETA_KEY_URL,
        }

    if not resolved.exists():
        return {
            "level": "error",
            "message": f"MakeMKV executable not found: {makemkv_path}",
            "details": [],
            "build_version": None,
            "can_rip": False,
            "beta_key_expires_at": None,
            "days_until_expiry": None,
            "checked_at": checked_at,
            "source_url": MAKEMKV_BETA_KEY_URL,
        }

    probe = _probe_makemkv_local_status(resolved)
    beta_expiry = _fetch_makemkv_beta_key_expiry()
    details = list(probe.get("details") or [])
    level = "ok"
    message = ""
    can_rip = True

    if probe.get("issue") == "expired":
        level = "error"
        can_rip = False
        message = (
            "MakeMKV cannot rip because its beta key expired or the installed version is too old. "
            "Update MakeMKV or enter a valid registration key."
        )
    elif probe.get("issue") == "probe_failed":
        level = "warning"
        can_rip = None
        message = "Could not verify MakeMKV rip readiness automatically."

    days_until_expiry = beta_expiry.get("days_until_expiry") if beta_expiry else None
    beta_key_expires_at = beta_expiry.get("beta_key_expires_at") if beta_expiry else None
    if beta_expiry and days_until_expiry is not None:
        if days_until_expiry < 0:
            if level != "error":
                level = "warning"
                message = "The published MakeMKV beta key is past its listed expiry date. Refresh your beta key if you rely on it."
            details.append(f"Published beta key expiry: {beta_key_expires_at}")
        elif days_until_expiry <= 14 and level == "ok":
            level = "warning"
            if days_until_expiry == 0:
                message = "The published MakeMKV beta key expires today."
            elif days_until_expiry == 1:
                message = "The published MakeMKV beta key expires in 1 day."
            else:
                message = f"The published MakeMKV beta key expires in {days_until_expiry} days."
            details.append(f"Published beta key expiry: {beta_key_expires_at}")
    elif probe.get("online_error"):
        details.append(f"Could not refresh beta-key expiry info: {probe['online_error']}")

    if level == "ok":
        message = "MakeMKV looks ready."

    return {
        "level": level,
        "message": message,
        "details": details,
        "build_version": probe.get("build_version"),
        "can_rip": can_rip,
        "beta_key_expires_at": beta_key_expires_at,
        "days_until_expiry": days_until_expiry,
        "checked_at": checked_at,
        "source_url": MAKEMKV_BETA_KEY_URL,
    }


def execute_rip_job(
    conn,
    cfg: AppConfig,
    job_id: str,
    optical_drive: str | None = None,
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
        source_spec = _build_makemkv_source_spec(optical_drive, disc_index)
        _ensure_drive_available(conn, job_id, optical_drive)
        removed = _clear_stale_rip_output(rip_output_dir)
        if removed:
            append_job_log(
                conn=conn,
                job_id=job_id,
                level="WARNING",
                message=f"Removed {removed} stale rip output file(s) before retrying MakeMKV.",
                from_status=None,
                to_status=None,
            )
            conn.commit()
        append_job_log(
            conn=conn,
            job_id=job_id,
            level="INFO",
            message=f"Starting MakeMKV disc-info scan{f' for {optical_drive}' if optical_drive else ''}.",
            from_status=None,
            to_status=None,
        )
        conn.commit()
        disc_info_text, disc_info_by_title = _read_makemkv_disc_info(
            makemkv_path=cfg.makemkv_path,
            source_spec=source_spec,
            timeout_seconds=_disc_scan_timeout_seconds(cfg, optical_drive),
            min_title_seconds=cfg.rip_min_title_seconds,
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

        selection = _plan_title_selection(conn, cfg, job_id, disc_info_by_title)

        _ensure_job_still_ripping(conn, job_id)
        append_job_log(
            conn=conn,
            job_id=job_id,
            level="INFO",
            message=f"Starting MakeMKV rip{f' from {optical_drive}' if optical_drive else ''} to {rip_output_dir}",
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
            source_spec=source_spec,
            timeout_seconds=cfg.rip_timeout_seconds,
            title_ids=selection,
            min_title_seconds=cfg.rip_min_title_seconds,
        )
        log_already_written = True
        if exit_code != 0:
            raise RipError(_describe_makemkv_failure(rip_log_text, makemkv_log_path))
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
    # The rip stage is over; leave no stale progress for the UI to show while
    # the job moves on to identify/map.
    clear_progress(conn, job_id)
    conn.commit()

    if not mock and cfg.eject_after_rip:
        ejected = eject_drive(optical_drive)
        append_job_log(
            conn=conn,
            job_id=job_id,
            level="INFO" if ejected else "WARNING",
            message=(
                f"Ejected {optical_drive or 'optical drive'} after rip."
                if ejected
                else f"Could not eject {optical_drive or 'optical drive'} after rip."
            ),
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


def _plan_title_selection(
    conn,
    cfg: AppConfig,
    job_id: str,
    disc_info_by_title: dict[int, dict[str, Any]],
) -> list[int] | None:
    """
    Decide which titles to rip. None means "everything" (the fast path).

    Ripping every title MakeMKV offers is the single largest avoidable cost in
    a collection migration: trailers, studio logos, language variants and
    "play all" tracks routinely add more data than the content itself.
    """
    if cfg.rip_title_selection == "all":
        return None
    if not disc_info_by_title:
        append_job_log(
            conn=conn,
            job_id=job_id,
            level="WARNING",
            message="No disc-info title data; ripping all titles.",
            from_status=None,
            to_status=None,
        )
        conn.commit()
        return None

    job = get_job(conn, job_id) or {}
    candidates = build_title_candidates(disc_info_by_title)
    selection = select_titles(
        candidates,
        media_type=str(job.get("media_type") or "tv"),
        movie_mode=str(job.get("movie_mode") or "single"),
        min_episode_minutes=cfg.min_episode_minutes,
        max_episode_minutes=cfg.max_episode_minutes,
    )

    append_job_log(
        conn=conn,
        job_id=job_id,
        level="INFO",
        message=f"Title selection: {selection.reason}",
        from_status=None,
        to_status=None,
    )
    if selection.skipped:
        append_job_log(
            conn=conn,
            job_id=job_id,
            level="INFO",
            message="Skipping " + "; ".join(selection.skipped),
            from_status=None,
            to_status=None,
        )
    conn.commit()

    if selection.is_everything or not selection.title_ids:
        return None
    return selection.title_ids


def _disc_scan_timeout_seconds(cfg: AppConfig, optical_drive: str | None) -> int:
    """
    How long to allow the pre-rip disc scan.

    A DVD scan finishes in seconds. A Blu-ray scan routinely takes one to three
    minutes because MakeMKV has to walk the playlist structure -- the previous
    45s cap meant Blu-ray scans timed out, which silently discarded all title
    metadata and forced a rip-everything fallback on exactly the discs where
    ripping everything is most expensive.
    """
    return int(min(cfg.rip_timeout_seconds, cfg.disc_scan_timeout_seconds))


def _run_makemkv_rip_streaming(
    conn,
    job_id: str,
    makemkv_path: str,
    output_dir: Path,
    log_path: Path,
    source_spec: str,
    timeout_seconds: int,
    title_ids: list[int] | None = None,
    min_title_seconds: int = 120,
) -> tuple[str, int]:
    makemkv = _resolve_makemkv_cli_path(makemkv_path)
    if not makemkv.exists():
        raise RipError(f"MakeMKV executable not found: {makemkv_path}")

    # One command per selected title, or a single "all" pass when we want the
    # whole disc. --minlength must match the value used for the disc-info scan:
    # MakeMKV renumbers titles after length filtering, so a different value
    # here would make the selected title IDs point at different titles.
    selectors: list[str] = [str(t) for t in title_ids] if title_ids else ["all"]

    start = time.monotonic()
    exit_code = 0

    with open(log_path, "w", encoding="utf-8") as lf:
        for index, selector in enumerate(selectors, start=1):
            _ensure_job_still_ripping(conn, job_id)
            cmd = [
                str(makemkv),
                "-r",
                "--progress=-same",
                f"--minlength={min_title_seconds}",
                "mkv",
                source_spec,
                selector,
                str(output_dir),
            ]
            log.info("running makemkv", extra={"command": " ".join(cmd)})
            lf.write(f"COMMAND: {' '.join(cmd)}\n\n")
            lf.flush()

            exit_code = _stream_one_makemkv_rip(
                conn=conn,
                job_id=job_id,
                cmd=cmd,
                log_file=lf,
                output_dir=output_dir,
                started_at=start,
                timeout_seconds=timeout_seconds,
                title_index=index,
                title_count=len(selectors),
            )
            lf.write(f"\nEXIT_CODE: {exit_code}\n")
            lf.flush()
            if exit_code != 0:
                break

    log_text = ""
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    return log_text, exit_code


def _stream_one_makemkv_rip(
    conn,
    job_id: str,
    cmd: list[str],
    log_file,
    output_dir: Path,
    started_at: float,
    timeout_seconds: int,
    title_index: int,
    title_count: int,
) -> int:
    """
    Run one MakeMKV command, teeing its robot output to the log while turning
    it into structured progress.

    MakeMKV reports exact progress on stdout (PRGV) along with the name of the
    operation it is performing (PRGC). Reading it is both more accurate and
    more responsive than the previous approach of measuring the output
    directory's size every 15 seconds.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise RipError(f"Failed to execute MakeMKV: {exc}") from exc

    operation = ""
    fraction = 0.0
    next_flush = 0.0
    last_size_mb = 0.0
    last_size_at = time.monotonic()
    rate_mb_s: float | None = None

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            log_file.write(line)

            now = time.monotonic()
            if now - started_at > timeout_seconds:
                proc.kill()
                raise RipError(f"MakeMKV timed out after {timeout_seconds}s.")

            event = parse_progress_line(line)
            if event is not None:
                if event.kind == "current_op" and event.text:
                    operation = event.text
                elif event.kind == "values":
                    computed = overall_fraction(event)
                    if computed is not None:
                        fraction = computed

            # Throttle: MakeMKV emits progress many times a second, and every
            # write here is a database commit competing with the UI's polling.
            if now < next_flush:
                continue
            next_flush = now + 1.0
            log_file.flush()

            if not _job_is_still_ripping(conn, job_id):
                proc.kill()
                raise RipError(f"Rip cancelled or no longer active for job {job_id}.")

            size_mb = _output_size_mb(output_dir)
            elapsed_since = max(0.001, now - last_size_at)
            if size_mb > last_size_mb:
                rate_mb_s = (size_mb - last_size_mb) / elapsed_since
                last_size_mb = size_mb
                last_size_at = now

            elapsed = now - started_at
            eta_seconds = None
            if fraction > 0.01:
                eta_seconds = max(0.0, elapsed * (1.0 - fraction) / fraction)

            detail = operation or "Ripping"
            if title_count > 1:
                detail = f"{detail} (title {title_index} of {title_count})"
            if size_mb > 0:
                detail = f"{detail} - {size_mb:,.0f} MB written"

            # Progress is reported against MakeMKV's own scale, and spread
            # across however many titles this rip covers.
            per_title = 1.0 / max(1, title_count)
            overall = ((title_index - 1) + fraction) * per_title

            upsert_progress(
                conn,
                job_id,
                stage="ripping",
                kind="ripping",
                current_units=round(overall * PROGRESS_MAX, 2),
                total_units=float(PROGRESS_MAX),
                unit="ticks",
                rate_per_second=rate_mb_s,
                eta_seconds=eta_seconds,
                detail=detail,
                title_index=title_index,
                title_count=title_count,
            )
            conn.execute(
                "UPDATE jobs SET updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            conn.commit()
    finally:
        if proc.stdout is not None:
            proc.stdout.close()

    return int(proc.wait() or 0)


def _output_size_mb(output_dir: Path) -> float:
    total_bytes = 0
    try:
        entries = list(output_dir.glob("*"))
    except OSError:
        return 0.0
    for entry in entries:
        try:
            total_bytes += int(entry.stat().st_size)
        except OSError:
            continue
    return total_bytes / (1024 * 1024)


def _describe_makemkv_failure(log_text: str, log_path: Path) -> str:
    lowered = log_text.lower()
    if "version is too old" in lowered or "temporary key has expired" in lowered:
        return (
            "MakeMKV cannot rip because its beta key expired or the installed version is too old. "
            f"Update MakeMKV or enter a valid registration key, then retry. See log: {log_path}"
        )
    return f"MakeMKV failed with non-zero exit code. See log: {log_path}"


def _probe_makemkv_local_status(makemkv: Path) -> dict[str, Any]:
    probe_root = Path.home()
    if not probe_root.exists():
        probe_root = Path.cwd()
    cmd = [str(makemkv), "-r", "info", f"file:{probe_root}"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "issue": "probe_failed",
            "details": ["MakeMKV readiness probe timed out."],
            "build_version": None,
        }
    except OSError as exc:
        return {
            "issue": "probe_failed",
            "details": [f"MakeMKV readiness probe failed: {exc}"],
            "build_version": None,
        }

    combined = f"{proc.stdout}\n{proc.stderr}".strip()
    lowered = combined.lower()
    version_match = re.search(r"MakeMKV v([0-9.]+)", combined)
    details: list[str] = []
    if proc.returncode not in (0, 10):
        details.append(f"Local MakeMKV probe exited with code {proc.returncode}.")
    if "version is too old" in lowered or "temporary key has expired" in lowered:
        return {
            "issue": "expired",
            "details": details,
            "build_version": version_match.group(1) if version_match else None,
        }
    return {
        "issue": None,
        "details": details,
        "build_version": version_match.group(1) if version_match else None,
    }


def _fetch_makemkv_beta_key_expiry() -> dict[str, Any] | None:
    request = urllib.request.Request(
        MAKEMKV_BETA_KEY_URL,
        headers={"User-Agent": "Auto-Ripper/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None

    parsed = _parse_beta_expiry_date(payload)
    if parsed is None:
        return None

    today = datetime.now(timezone.utc).date()
    return {
        "beta_key_expires_at": datetime.combine(parsed, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
        "days_until_expiry": (parsed - today).days,
    }


def _parse_beta_expiry_date(payload: str) -> date | None:
    end_of_month = re.search(
        r"valid until end of ([A-Za-z]+)\s+(\d{4})",
        payload,
        flags=re.IGNORECASE,
    )
    if end_of_month:
        month_name = end_of_month.group(1)
        year = int(end_of_month.group(2))
        month = _month_name_to_number(month_name)
        if month is None:
            return None
        day = calendar.monthrange(year, month)[1]
        return date(year, month, day)

    exact_day = re.search(
        r"valid until ([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})",
        payload,
        flags=re.IGNORECASE,
    )
    if exact_day:
        month = _month_name_to_number(exact_day.group(1))
        if month is None:
            return None
        return date(int(exact_day.group(3)), month, int(exact_day.group(2)))
    return None


def _month_name_to_number(value: str) -> int | None:
    normalized = value.strip().lower()
    for idx, month_name in enumerate(calendar.month_name):
        if month_name and month_name.lower() == normalized:
            return idx
    return None


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
    source_spec: str,
    timeout_seconds: int,
    min_title_seconds: int = 120,
) -> tuple[str, dict[int, dict[str, Any]]]:
    makemkv = _resolve_makemkv_cli_path(makemkv_path)
    if not makemkv.exists():
        return "", {}
    # --minlength must match the value the rip uses. MakeMKV numbers titles
    # after length filtering, so scanning with one value and ripping with
    # another would make the selected title IDs refer to different titles.
    cmd = [str(makemkv), "-r", f"--minlength={min_title_seconds}", "info", source_spec]
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


def _build_makemkv_source_spec(optical_drive: str | None, disc_index: int) -> str:
    normalized = (optical_drive or "").strip().rstrip("\\/")
    if normalized:
        return f"dev:{normalized}"
    return f"disc:{disc_index}"


def _ensure_drive_available(conn, job_id: str, optical_drive: str | None) -> None:
    normalized = (optical_drive or "").strip().upper()
    if not normalized:
        return
    conflict = conn.execute(
        """
        SELECT id
        FROM jobs
        WHERE id != ? AND status = 'ripping' AND UPPER(COALESCE(optical_drive, '')) = ?
        LIMIT 1
        """,
        (job_id, normalized),
    ).fetchone()
    if conflict:
        raise RipError(f"Optical drive {normalized} is already being used by job {conflict['id']}.")


def _clear_stale_rip_output(rip_output_dir: Path) -> int:
    removed = 0
    if not rip_output_dir.exists():
        return removed
    for path in rip_output_dir.iterdir():
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            raise RipError(f"Could not clear stale rip output file {path}: {exc}") from exc
    return removed


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

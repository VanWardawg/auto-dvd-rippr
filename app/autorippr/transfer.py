import hashlib
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .progress import clear_progress, upsert_progress
from .state import append_job_log


class TransferError(RuntimeError):
    pass


INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*]')


def ensure_nas_available(cfg: AppConfig) -> None:
    """
    Fail early and clearly when the NAS is not reachable.

    Without this the first sign of trouble is a mkdir deep inside the copy
    loop, after the rip and the rename have already run -- reported as a raw
    WinError. A disconnected mapped drive is the common case (the letter still
    exists in the registry but resolves to nothing), and it is worth naming
    explicitly because the fix is "reconnect it and hit Resume".
    """
    root_value = str(cfg.nas_root).strip()
    if not root_value:
        raise TransferError("No NAS root is configured. Set nas_root in Settings.")

    root = Path(root_value)
    try:
        exists = root.exists()
    except OSError as exc:
        raise TransferError(
            f"NAS root {root_value} is not reachable ({exc}). "
            "Reconnect it and resume the job."
        ) from exc

    if not exists:
        raise TransferError(
            f"NAS root {root_value} is not reachable. If it is a mapped network "
            "drive it may have been disconnected -- reconnect it and resume the job."
        )
    if not root.is_dir():
        raise TransferError(f"NAS root {root_value} exists but is not a folder.")


def transfer_job_outputs(conn, cfg: AppConfig, job_id: str) -> dict[str, Any]:
    ensure_nas_available(cfg)
    rows = conn.execute(
        """
        SELECT id, local_path, nas_path, transfer_status, transfer_attempts
        FROM outputs
        WHERE job_id = ?
        ORDER BY id ASC
        """,
        (job_id,),
    ).fetchall()
    if not rows:
        raise TransferError("No outputs found for transfer.")

    job = conn.execute(
        "SELECT media_type FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not job:
        raise TransferError("Job not found for NAS transfer.")

    copied = []
    errors = []
    finalize_root = Path(cfg.staging_root) / "jobs" / job_id / "finalized"
    for row in rows:
        out_id = int(row["id"])
        local_path = Path(str(row["local_path"]))
        if not local_path.exists():
            _record_failure(conn, out_id, "Local output missing")
            errors.append({"output_id": out_id, "error": "local_missing"})
            continue

        relative_dest = _build_relative_destination(local_path, finalize_root, str(job["media_type"] or "movie"))
        nas_final = Path(cfg.nas_root) / relative_dest
        temp_dest = nas_final.with_suffix(nas_final.suffix + ".part")
        try:
            nas_final.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TransferError(
                f"NAS destination is unavailable: {nas_final.parent} ({exc})"
            ) from exc
        recorded_nas_path = str(row["nas_path"] or "").strip()
        if nas_final.exists() and recorded_nas_path != str(nas_final):
            message = f"destination_exists:{nas_final}"
            append_job_log(
                conn,
                job_id,
                "ERROR",
                (
                    "NAS destination already exists; refusing to overwrite. "
                    "Review TMDB selection or move/remap the existing remote file first."
                ),
                None,
                None,
            )
            _record_failure(conn, out_id, message)
            errors.append({"output_id": out_id, "error": message})
            continue

        ok, checksum, err = _copy_with_retry(
            conn=conn,
            job_id=job_id,
            output_id=out_id,
            source=local_path,
            temp_dest=temp_dest,
            final_dest=nas_final,
            retries=cfg.transfer_retry_count,
            backoff_seconds=cfg.transfer_backoff_seconds,
            verify=cfg.verify_transfers,
        )
        if not ok:
            _record_failure(conn, out_id, err or "copy_failed")
            errors.append({"output_id": out_id, "error": err or "copy_failed"})
            continue

        conn.execute(
            """
            UPDATE outputs
            SET nas_path = ?, checksum_sha256 = ?, transfer_status = 'done',
                transfer_attempts = transfer_attempts + 1, last_error = NULL
            WHERE id = ?
            """,
            (str(nas_final), checksum, out_id),
        )
        _record_attempt(conn, out_id, "success", None)
        copied.append({"output_id": out_id, "nas_path": str(nas_final), "checksum_sha256": checksum})
        conn.commit()

    clear_progress(conn, job_id)
    append_job_log(
        conn,
        job_id,
        "ERROR" if errors else "INFO",
        f"NAS transfer complete: copied={len(copied)} failed={len(errors)}",
        None,
        None,
    )
    conn.commit()
    return {"job_id": job_id, "copied": copied, "errors": errors}


def _copy_with_retry(
    conn,
    job_id: str,
    output_id: int,
    source: Path,
    temp_dest: Path,
    final_dest: Path,
    retries: int,
    backoff_seconds: int,
    verify: bool = False,
) -> tuple[bool, str | None, str | None]:
    """
    Copy one file to the NAS, hashing it as it streams past.

    The hash used to be computed by reading the finished file back off the
    NAS, which on a 5.5 GB movie meant pulling every byte back over SMB and
    roughly doubling the transfer time. It also verified nothing: the source
    was never hashed, so the digest was recorded but never compared against
    anything, and corruption could not have been detected.

    Hashing the source stream as it is copied costs nothing. When `verify` is
    set the destination is additionally read back and *compared* to that hash,
    so the expensive read now buys real end-to-end verification instead of
    being pure overhead.
    """
    last_error = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            if temp_dest.exists():
                _safe_unlink(temp_dest)
            total_bytes = max(1, source.stat().st_size)
            copied_bytes = 0
            chunk_size = 8 * 1024 * 1024
            digest = hashlib.sha256()
            start = time.monotonic()
            next_heartbeat = start + 5.0
            with open(source, "rb") as src, open(temp_dest, "wb") as dst:
                while True:
                    chunk = src.read(chunk_size)
                    if not chunk:
                        break
                    dst.write(chunk)
                    digest.update(chunk)
                    copied_bytes += len(chunk)
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        elapsed = max(0.001, now - start)
                        rate = copied_bytes / elapsed
                        _emit_transfer_heartbeat(conn, job_id, output_id, copied_bytes, total_bytes, rate)
                        next_heartbeat = now + 5.0
            checksum = digest.hexdigest()
            shutil.copystat(source, temp_dest)
            if temp_dest.stat().st_size != source.stat().st_size:
                raise TransferError("size_mismatch_after_copy")
            if verify:
                _emit_transfer_verify_heartbeat(conn, job_id, copied_bytes, total_bytes)
                written = _sha256(temp_dest)
                if written != checksum:
                    raise TransferError("checksum_mismatch_after_copy")
            temp_dest.replace(final_dest)
            if final_dest.stat().st_size != source.stat().st_size:
                raise TransferError("size_mismatch_after_rename")
            return True, checksum, None
        except Exception as exc:
            last_error = str(exc)
            if temp_dest.exists():
                try:
                    _safe_unlink(temp_dest)
                except OSError as cleanup_exc:
                    last_error = f"{last_error}; cleanup_failed={cleanup_exc}"
            if attempt < retries:
                time.sleep(max(1, backoff_seconds) * attempt)
    return False, None, last_error


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _emit_transfer_heartbeat(
    conn,
    job_id: str,
    output_id: int,
    copied_bytes: int,
    total_bytes: int,
    rate_bytes_per_sec: float,
) -> None:
    copied_mb = copied_bytes / (1024 * 1024)
    total_mb = total_bytes / (1024 * 1024)
    rate_mb_s = rate_bytes_per_sec / (1024 * 1024)
    remaining_seconds = max(0.0, (total_bytes - copied_bytes) / max(rate_bytes_per_sec, 1.0))
    upsert_progress(
        conn,
        job_id,
        stage="copying",
        kind="copying",
        current_units=copied_mb,
        total_units=total_mb,
        unit="mb",
        rate_per_second=rate_mb_s,
        eta_seconds=remaining_seconds,
        detail=f"Copying {copied_mb:.1f} / {total_mb:.1f} MB",
    )
    conn.commit()


def _emit_transfer_verify_heartbeat(conn, job_id: str, copied_bytes: int, total_bytes: int) -> None:
    """Keep the UI honest about the read-back, which is not instant."""
    total_mb = total_bytes / (1024 * 1024)
    upsert_progress(
        conn,
        job_id,
        stage="copying",
        kind="copying",
        current_units=copied_bytes / (1024 * 1024),
        total_units=total_mb,
        unit="mb",
        detail=f"Verifying {total_mb:.1f} MB on the NAS",
    )
    conn.commit()


def _build_relative_destination(local_path: Path, finalize_root: Path, media_type: str) -> Path:
    try:
        relative = local_path.relative_to(finalize_root)
    except ValueError as exc:
        raise TransferError(f"Output path is outside finalized root: {local_path}") from exc
    if media_type == "movie":
        return Path("Movies") / relative
    return Path("TVShows") / relative


def _sanitize_name(value: str) -> str:
    cleaned = INVALID_PATH_CHARS.sub("", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:180] if len(cleaned) > 180 else cleaned


def _record_failure(conn, output_id: int, error_message: str) -> None:
    conn.execute(
        """
        UPDATE outputs
        SET transfer_status = 'error',
            transfer_attempts = transfer_attempts + 1,
            last_error = ?
        WHERE id = ?
        """,
        (error_message, output_id),
    )
    _record_attempt(conn, output_id, "error", error_message)
    conn.commit()


def _record_attempt(conn, output_id: int, status: str, error_message: str | None) -> None:
    attempt_no = conn.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n FROM transfer_attempts WHERE output_id = ?",
        (output_id,),
    ).fetchone()["n"]
    conn.execute(
        """
        INSERT INTO transfer_attempts (output_id, attempt_number, status, error_message, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (output_id, int(attempt_no), status, error_message, datetime.now(timezone.utc).isoformat()),
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

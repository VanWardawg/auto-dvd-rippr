import uuid
from datetime import datetime, timezone
from typing import Iterable

from .logger import get_logger


log = get_logger("state")

ALL_STATUSES = (
    "queued",
    "ripping",
    "identifying",
    "mapping",
    "splitting",
    "renaming",
    "copying",
    "done",
    "error",
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"ripping", "error"},
    "ripping": {"identifying", "error"},
    "identifying": {"mapping", "renaming", "error"},
    "mapping": {"splitting", "renaming", "error"},
    "splitting": {"renaming", "error"},
    "renaming": {"copying", "error"},
    "copying": {"done", "error"},
    "done": set(),
    # Retrying an errored job re-enters at the stage it failed in, rather than
    # starting over: a transient failure late in the pipeline (an unmounted NAS
    # during the copy) must not force a fresh rip of a disc that already
    # succeeded. pipeline._infer_resume_stage decides which stage that is from
    # the artifacts on record.
    "error": {"queued", "ripping", "identifying", "mapping", "splitting", "renaming", "copying"},
}


class InvalidTransitionError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_valid_status(value: str) -> bool:
    return value in ALL_STATUSES


def can_transition(from_status: str, to_status: str) -> bool:
    if from_status not in ALLOWED_TRANSITIONS:
        return False
    return to_status in ALLOWED_TRANSITIONS[from_status]


def create_job(
    conn,
    disc_label: str = "",
    optical_drive: str | None = None,
    media_type: str = "tv",
    movie_mode: str = "single",
    disc_scope: str | None = None,
    season_number: int | None = None,
    episode_range_start: int | None = None,
    episode_range_end: int | None = None,
) -> str:
    job_id = str(uuid.uuid4())
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO jobs (
            id, disc_label, optical_drive, media_type, movie_mode, disc_scope, season_number, episode_range_start, episode_range_end,
            status, current_stage, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?)
        """,
        (
            job_id,
            disc_label,
            optical_drive,
            media_type,
            movie_mode,
            disc_scope,
            season_number,
            episode_range_start,
            episode_range_end,
            ts,
            ts,
        ),
    )
    append_job_log(
        conn=conn,
        job_id=job_id,
        level="INFO",
        message="Job created",
        from_status=None,
        to_status="queued",
    )
    conn.commit()
    return job_id


def update_job_disc_profile(
    conn,
    job_id: str,
    disc_scope: str | None,
    movie_mode: str | None,
    season_number: int | None,
    episode_range_start: int | None,
    episode_range_end: int | None,
) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET disc_scope = ?, movie_mode = COALESCE(?, movie_mode), season_number = ?, episode_range_start = ?, episode_range_end = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            disc_scope,
            movie_mode,
            season_number,
            episode_range_start,
            episode_range_end,
            now_iso(),
            job_id,
        ),
    )
    append_job_log(
        conn=conn,
        job_id=job_id,
        level="INFO",
        message=(
            f"Disc profile set: scope={disc_scope or 'unspecified'}, movie_mode={movie_mode or 'unchanged'}, "
            f"season={season_number}, range={episode_range_start}-{episode_range_end}"
        ),
        from_status=None,
        to_status=None,
    )
    conn.commit()


def transition_job(conn, job_id: str, to_status: str, error_message: str | None = None) -> None:
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise InvalidTransitionError(f"Job not found: {job_id}")

    from_status = str(row["status"])
    if not is_valid_status(to_status):
        raise InvalidTransitionError(f"Invalid status '{to_status}'.")

    if not can_transition(from_status, to_status):
        raise InvalidTransitionError(
            f"Invalid transition {from_status} -> {to_status}. "
            f"Allowed: {sorted(ALLOWED_TRANSITIONS.get(from_status, set()))}"
        )

    ts = now_iso()
    # Any transition means the job moved on, so it is no longer waiting on a
    # person -- clearing it here means no caller can forget to.
    conn.execute(
        """
        UPDATE jobs
        SET status = ?, current_stage = ?, updated_at = ?, error_message = ?,
            awaiting_review = 0
        WHERE id = ?
        """,
        (to_status, to_status, ts, error_message, job_id),
    )

    append_job_log(
        conn=conn,
        job_id=job_id,
        level="ERROR" if to_status == "error" else "INFO",
        message=f"Transitioned {from_status} -> {to_status}",
        from_status=from_status,
        to_status=to_status,
    )
    conn.commit()
    log.info(
        "state transition",
        extra={"job_id": job_id, "from_status": from_status, "to_status": to_status},
    )


def append_job_log(
    conn,
    job_id: str,
    level: str,
    message: str,
    from_status: str | None,
    to_status: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO job_logs (job_id, timestamp, level, message, from_status, to_status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job_id, now_iso(), level, message, from_status, to_status),
    )


def set_awaiting_review(conn, job_id: str, waiting: bool) -> None:
    """Record whether a job is stopped and waiting on a person."""
    conn.execute(
        "UPDATE jobs SET awaiting_review = ?, updated_at = ? WHERE id = ?",
        (1 if waiting else 0, now_iso(), job_id),
    )
    conn.commit()


def get_job(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def list_job_logs(conn, job_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM job_logs WHERE job_id = ? ORDER BY id ASC",
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def transition_chain(conn, job_id: str, steps: Iterable[str]) -> None:
    for status in steps:
        transition_job(conn, job_id, status)


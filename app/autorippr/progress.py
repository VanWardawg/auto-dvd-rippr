"""
Structured per-job progress.

Progress used to be written as free-text log lines ("Rip heartbeat: size_mb=12")
and parsed back out by the UI layer with a regex. That bloated job_logs with
thousands of rows per rip and made the UI's progress bar depend on log message
wording. This module owns a single upserted row per job instead.

A row is transient: it describes what a job is doing *right now*, and is
cleared when the job leaves the stage. Anything worth keeping afterwards
belongs in job_logs.
"""

from typing import Any

from .state import now_iso


def upsert_progress(
    conn,
    job_id: str,
    *,
    stage: str,
    kind: str | None = None,
    current_units: float | None = None,
    total_units: float | None = None,
    unit: str = "mb",
    rate_per_second: float | None = None,
    eta_seconds: float | None = None,
    detail: str | None = None,
    title_index: int | None = None,
    title_count: int | None = None,
) -> None:
    """Record the current progress of a job, replacing any previous row."""
    conn.execute(
        """
        INSERT INTO job_progress (
            job_id, stage, kind, current_units, total_units, unit,
            rate_per_second, eta_seconds, detail, title_index, title_count,
            updated_at, last_advance_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            stage = excluded.stage,
            kind = excluded.kind,
            current_units = excluded.current_units,
            total_units = excluded.total_units,
            unit = excluded.unit,
            rate_per_second = excluded.rate_per_second,
            eta_seconds = excluded.eta_seconds,
            detail = excluded.detail,
            title_index = excluded.title_index,
            title_count = excluded.title_count,
            updated_at = excluded.updated_at,
            -- Only advance the stall clock when the work actually moved, or
            -- when the job switched stage (a new stage starts fresh).
            last_advance_at = CASE
                WHEN excluded.stage != job_progress.stage THEN excluded.updated_at
                WHEN job_progress.current_units IS NULL THEN excluded.updated_at
                WHEN excluded.current_units > job_progress.current_units THEN excluded.updated_at
                ELSE job_progress.last_advance_at
            END
        """,
        (
            job_id,
            stage,
            kind,
            current_units,
            total_units,
            unit,
            rate_per_second,
            eta_seconds,
            detail,
            title_index,
            title_count,
            now_iso(),
            now_iso(),
        ),
    )


def get_progress(conn, job_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM job_progress WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def clear_progress(conn, job_id: str) -> None:
    """Drop a job's progress row once it finishes or leaves the stage."""
    conn.execute("DELETE FROM job_progress WHERE job_id = ?", (job_id,))


def fraction(progress: dict[str, Any] | None) -> float | None:
    """Completed fraction in [0, 0.99], or None when it cannot be computed."""
    if not progress:
        return None
    total = progress.get("total_units")
    current = progress.get("current_units")
    if not total or current is None:
        return None
    try:
        total_value = float(total)
        current_value = float(current)
    except (TypeError, ValueError):
        return None
    if total_value <= 0:
        return None
    # Cap below 1.0: a stage is not complete until the pipeline says so, and a
    # bar that sits at 100% while work continues reads as a hang.
    return min(0.99, max(0.0, current_value / total_value))

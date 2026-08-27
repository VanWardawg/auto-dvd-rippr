"""
Tests for surviving a crash in the detached pipeline process.

All of this comes from one real incident. Two jobs were ripping; the first
finished and began its finalize/NAS-copy burst; six seconds later the second
job's backend process died. Because the exception was not one of the domain
errors the pipeline knows about, nothing marked the job failed. The result was
a zombie: the UI showed `ripping` forever, MakeMKV kept spinning the disc with
nobody reading its output, Resume was unavailable because Resume only applies
to errored jobs, and the traceback went to a discarded stderr so there was no
record of what happened.

The three defects, each covered below:
  1. only domain exceptions marked a job errored
  2. the stall check compared the progress row against itself, so a dead
     writer -- whose timestamps all freeze together -- read as healthy
  3. a crash left no diagnosable trace
"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import main as cli  # noqa: E402
from autorippr.config import AppConfig  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.pipeline import run_pipeline_for_job  # noqa: E402
from autorippr.state import can_transition, create_job, get_job, transition_job  # noqa: E402

NOW = "2026-08-24T00:00:00+00:00"


def build_config(root: Path) -> AppConfig:
    return AppConfig(
        tmdb_api_key="test-key",
        makemkv_path=r"C:\mk.exe",
        ffmpeg_path=r"C:\ffmpeg.exe",
        ffprobe_path=r"C:\ffprobe.exe",
        staging_root=str(root),
        nas_root=str(root / "nas"),
        db_path=str(root / "autorippr.db"),
        log_path=str(root / "autorippr.log"),
    )


def fail_job(conn, job_id: str, at_stage: str) -> None:
    order = ["queued", "ripping", "identifying", "mapping", "splitting", "renaming", "copying"]
    current = "queued"
    for stage in order[1 : order.index(at_stage) + 1]:
        if can_transition(current, stage):
            transition_job(conn, job_id, stage)
            current = stage
    transition_job(conn, job_id, "error", "boom")
    conn.commit()


class UnexpectedExceptionTests(unittest.TestCase):
    """A crash must leave the job resumable, not stuck mid-stage."""

    def _job_that_crashes(self, conn, cfg, exc: BaseException):
        job_id = create_job(conn, disc_label="DISC", media_type="movie")
        conn.execute(
            "INSERT INTO outputs (job_id, local_path, transfer_status) VALUES (?,?,?)",
            (job_id, str(Path(cfg.staging_root) / "movie.mkv"), "pending"),
        )
        fail_job(conn, job_id, "copying")
        with patch("autorippr.pipeline.transfer_job_outputs", side_effect=exc):
            with self.assertRaises(type(exc)):
                run_pipeline_for_job(conn, cfg, job_id)
        return job_id

    def test_a_locked_database_marks_the_job_errored(self) -> None:
        # The actual incident: sqlite3.OperationalError is not a domain error,
        # so it escaped both handlers and left the job in its active status.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_config(Path(tmp))
            conn = open_db(cfg.db_path)
            try:
                job_id = self._job_that_crashes(
                    conn, cfg, sqlite3.OperationalError("database is locked")
                )
                self.assertEqual(get_job(conn, job_id)["status"], "error")
            finally:
                conn.close()

    def test_the_recorded_error_names_the_exception(self) -> None:
        # "OperationalError: database is locked" is the whole diagnosis; a
        # bare "failed" would have left this incident just as opaque.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_config(Path(tmp))
            conn = open_db(cfg.db_path)
            try:
                job_id = self._job_that_crashes(
                    conn, cfg, sqlite3.OperationalError("database is locked")
                )
                message = str(get_job(conn, job_id).get("error_message") or "")
                self.assertIn("OperationalError", message)
                self.assertIn("database is locked", message)
            finally:
                conn.close()

    def test_an_arbitrary_bug_is_also_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_config(Path(tmp))
            conn = open_db(cfg.db_path)
            try:
                job_id = self._job_that_crashes(conn, cfg, ValueError("some bug"))
                self.assertEqual(get_job(conn, job_id)["status"], "error")
            finally:
                conn.close()

    def test_the_original_exception_survives(self) -> None:
        # The crash must propagate unchanged. Swallowing it would turn a
        # visible failure into a silent one, which is the bug in reverse.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_config(Path(tmp))
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="DISC", media_type="movie")
                conn.execute(
                    "INSERT INTO outputs (job_id, local_path, transfer_status) VALUES (?,?,?)",
                    (job_id, str(Path(tmp) / "movie.mkv"), "pending"),
                )
                fail_job(conn, job_id, "copying")
                with patch(
                    "autorippr.pipeline.transfer_job_outputs",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        run_pipeline_for_job(conn, cfg, job_id)
                self.assertIn("database is locked", str(caught.exception))
            finally:
                conn.close()

    def test_a_failure_to_record_does_not_mask_the_crash(self) -> None:
        # If the database is the thing that broke, writing the error state
        # fails too -- and the replacement exception would hide the real one.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_config(Path(tmp))
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="DISC", media_type="movie")
                conn.execute(
                    "INSERT INTO outputs (job_id, local_path, transfer_status) VALUES (?,?,?)",
                    (job_id, str(Path(tmp) / "movie.mkv"), "pending"),
                )
                fail_job(conn, job_id, "copying")
                with patch(
                    "autorippr.pipeline.transfer_job_outputs",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ), patch(
                    "autorippr.pipeline._transition_job_to_error_if_active",
                    side_effect=sqlite3.OperationalError("still locked"),
                ), patch("autorippr.pipeline.log"):  # the failure is logged; keep test output clean
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        run_pipeline_for_job(conn, cfg, job_id)
                self.assertIn("database is locked", str(caught.exception))
            finally:
                conn.close()


def progress_row(*, age_seconds: float, advanced_seconds_ago: float | None = None):
    """
    A progress row last written `age_seconds` ago.

    `advanced_seconds_ago` defaults to the same instant, which is the shape a
    dead writer leaves behind: every timestamp frozen together.
    """
    updated = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    if advanced_seconds_ago is None:
        advance = updated
    else:
        advance = datetime.now(timezone.utc) - timedelta(seconds=advanced_seconds_ago)
    return {
        "stage": "ripping",
        "current_units": 10948.0,
        "total_units": 65536.0,
        "updated_at": updated.isoformat(),
        "last_advance_at": advance.isoformat(),
    }


class HeartbeatTests(unittest.TestCase):
    def test_a_stale_row_is_measured_against_the_clock(self) -> None:
        self.assertGreater(cli._seconds_since_heartbeat(progress_row(age_seconds=600)), 500)

    def test_a_fresh_row_reads_as_current(self) -> None:
        self.assertLess(cli._seconds_since_heartbeat(progress_row(age_seconds=1)), 30)

    def test_no_row_is_not_an_answer(self) -> None:
        self.assertIsNone(cli._seconds_since_heartbeat(None))

    def test_the_old_check_cannot_see_a_dead_writer(self) -> None:
        # Documents precisely why the new check had to exist: a row frozen ten
        # minutes ago still reports zero seconds since it last advanced,
        # because it is being compared against itself.
        frozen = progress_row(age_seconds=600)
        self.assertEqual(cli._seconds_since_last_advance(frozen), 0.0)
        self.assertGreater(cli._seconds_since_heartbeat(frozen), 500)


class AbandonedRipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / "jobs" / "j" / "logs").mkdir(parents=True, exist_ok=True)
        (root / "jobs" / "j" / "logs" / "makemkv.log").write_text("", encoding="utf-8")
        self.cfg = SimpleNamespace(staging_root=str(root))

    def _review(self, row):
        return cli._build_review_state(
            cfg=self.cfg,
            job={"id": "j", "status": "ripping", "media_type": "movie", "updated_at": NOW},
            logs=[],
            progress_row=row,
            selected_media=None,
            selected_movies=[],
            tmdb_rows=[],
            mapping_rows=[],
            rip_title_rows=[],
            bundle_association=None,
            tmdb_threshold=0.75,
        )["rip"]

    def test_an_abandoned_rip_is_flagged(self) -> None:
        rip = self._review(progress_row(age_seconds=600))
        self.assertTrue(rip["needed"])
        self.assertIn("abandoned", rip["reason"].lower())

    def test_it_says_makemkv_may_still_be_running(self) -> None:
        # The practical consequence for the user: a drive is still spinning.
        rip = self._review(progress_row(age_seconds=600))
        self.assertIn("makemkv", rip["reason"].lower())

    def test_a_live_rip_is_left_alone(self) -> None:
        self.assertFalse(self._review(progress_row(age_seconds=2))["needed"])

    def test_a_slow_disc_is_stalled_not_abandoned(self) -> None:
        # Still heartbeating, just not advancing -- a worn disc, not a crash.
        # The two need different words because they need different remedies.
        rip = self._review(progress_row(age_seconds=1, advanced_seconds_ago=300))
        self.assertTrue(rip["needed"])
        self.assertIn("stalled", rip["reason"].lower())
        self.assertNotIn("abandoned", rip["reason"].lower())


if __name__ == "__main__":
    unittest.main()


class TruncatedLogRecoveryTests(unittest.TestCase):
    """
    A rip whose log died but whose file finished must not be re-ripped.

    From the same incident: the pipeline process died at 02:55, so makemkv.log
    stops mid-PRGV and never gets MakeMKV's "Copy complete." line. But MakeMKV
    itself carried on as an orphan for another 25 minutes and wrote a complete,
    playable 4.77 GB file -- 4511s against the 4510s the disc scan predicted.
    Recovery keyed on the log marker alone, so it would have thrown that away
    and re-ripped an 84-minute disc.
    """

    # The shape _parse_makemkv_info_output actually returns: attributes nested
    # under "fields", keyed by MakeMKV's numeric attribute codes (9 = duration).
    DISC_INFO = {
        0: {
            "fields": {"8": "18", "9": "1:15:10", "10": "4.7 GB", "11": "5124672022"},
            "display_name": "C1_t00.mkv",
        }
    }

    def _titles(self, seconds):
        return [SimpleNamespace(title_id=0, duration_seconds=seconds, chapter_count=12,
                                source_file="C1_t00.mkv")]

    def test_a_finished_file_is_accepted_without_the_marker(self) -> None:
        from autorippr import rip
        self.assertTrue(rip._durations_match_the_disc(self._titles(4511.97), self.DISC_INFO))

    def test_a_rip_cut_short_is_rejected(self) -> None:
        # The case the marker existed to catch: a partial file must still be
        # refused, or recovery would register a truncated movie as complete.
        from autorippr import rip
        self.assertFalse(rip._durations_match_the_disc(self._titles(1200.0), self.DISC_INFO))

    def test_a_file_slightly_short_of_the_disc_is_still_fine(self) -> None:
        from autorippr import rip
        self.assertTrue(rip._durations_match_the_disc(self._titles(4505.0), self.DISC_INFO))

    def test_without_disc_info_it_refuses_to_guess(self) -> None:
        from autorippr import rip
        self.assertFalse(rip._durations_match_the_disc(self._titles(4511.97), {}))

    def test_an_unprobeable_file_is_rejected(self) -> None:
        from autorippr import rip
        self.assertFalse(rip._durations_match_the_disc(self._titles(0), self.DISC_INFO))

    def test_one_bad_title_among_good_ones_rejects_the_lot(self) -> None:
        from autorippr import rip
        titles = self._titles(4511.97) + [
            SimpleNamespace(title_id=1, duration_seconds=90.0, chapter_count=1, source_file="b.mkv")
        ]
        self.assertFalse(rip._durations_match_the_disc(titles, self.DISC_INFO))

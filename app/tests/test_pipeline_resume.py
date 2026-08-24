"""
Tests for resuming a job that failed.

The failure these guard against is silent: an errored job matched no stage in
run_pipeline_for_job, so it fell through every branch and returned without
doing anything. The UI's Resume button appeared to work and changed nothing.

The other half is just as important -- resuming must not redo expensive work.
A NAS that was unmounted during the copy must not cost a fresh rip of a disc
that already succeeded.
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.config import AppConfig  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.pipeline import _infer_resume_stage, run_pipeline_for_job  # noqa: E402
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


def add_rip_title(conn, job_id: str) -> int:
    conn.execute(
        "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
        (job_id, 0, 5040.0, "t00.mkv"),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])


def select_media(conn, job_id: str, media_type: str) -> None:
    conn.execute(
        "INSERT INTO job_selected_media (job_id, media_type, tmdb_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (job_id, media_type, 1, "Title", NOW, NOW),
    )


def fail_job(conn, job_id: str, at_stage: str) -> None:
    """Drive a job to `at_stage` and then error out, as a real failure would."""
    order = ["queued", "ripping", "identifying", "mapping", "splitting", "renaming", "copying"]
    current = "queued"
    for stage in order[1 : order.index(at_stage) + 1]:
        if can_transition(current, stage):
            transition_job(conn, job_id, stage)
            current = stage
    transition_job(conn, job_id, "error", "boom")
    conn.commit()


class InferResumeStageTests(unittest.TestCase):
    def _job(self, conn, **kwargs):
        return create_job(conn, disc_label="DISC", **kwargs)

    def test_finalized_outputs_resume_at_the_copy(self) -> None:
        """The expensive-work case: never re-rip because the NAS was down."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(str(Path(tmp) / "a.db"))
            try:
                job_id = self._job(conn, media_type="movie")
                add_rip_title(conn, job_id)
                select_media(conn, job_id, "movie")
                conn.execute(
                    "INSERT INTO outputs (job_id, local_path, transfer_status) VALUES (?,?,?)",
                    (job_id, r"C:\finalized\movie.mkv", "pending"),
                )
                fail_job(conn, job_id, "copying")
                self.assertEqual(_infer_resume_stage(conn, get_job(conn, job_id)), "copying")
            finally:
                conn.close()

    def test_movie_with_selection_resumes_at_renaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(str(Path(tmp) / "a.db"))
            try:
                job_id = self._job(conn, media_type="movie")
                add_rip_title(conn, job_id)
                select_media(conn, job_id, "movie")
                fail_job(conn, job_id, "renaming")
                self.assertEqual(_infer_resume_stage(conn, get_job(conn, job_id)), "renaming")
            finally:
                conn.close()

    def test_ripped_but_unidentified_resumes_at_identifying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(str(Path(tmp) / "a.db"))
            try:
                job_id = self._job(conn, media_type="movie")
                add_rip_title(conn, job_id)
                fail_job(conn, job_id, "identifying")
                self.assertEqual(_infer_resume_stage(conn, get_job(conn, job_id)), "identifying")
            finally:
                conn.close()

    def test_nothing_ripped_starts_over(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(str(Path(tmp) / "a.db"))
            try:
                job_id = self._job(conn, media_type="movie")
                fail_job(conn, job_id, "ripping")
                self.assertEqual(_infer_resume_stage(conn, get_job(conn, job_id)), "queued")
            finally:
                conn.close()

    def test_tv_without_mappings_resumes_at_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(str(Path(tmp) / "a.db"))
            try:
                job_id = self._job(conn, media_type="tv")
                add_rip_title(conn, job_id)
                select_media(conn, job_id, "tv")
                fail_job(conn, job_id, "mapping")
                self.assertEqual(_infer_resume_stage(conn, get_job(conn, job_id)), "mapping")
            finally:
                conn.close()

    def test_tv_with_unfinished_splits_resumes_at_splitting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(str(Path(tmp) / "a.db"))
            try:
                job_id = self._job(conn, media_type="tv")
                rip_id = add_rip_title(conn, job_id)
                select_media(conn, job_id, "tv")
                conn.execute(
                    "INSERT INTO episode_mappings (job_id, rip_title_id, season_number, episode_start, needs_split) "
                    "VALUES (?,?,?,?,1)",
                    (job_id, rip_id, 1, 1),
                )
                conn.execute(
                    "INSERT INTO split_plans (job_id, source_file, segment_index, status) VALUES (?,?,?,?)",
                    (job_id, "t00.mkv", 0, "pending"),
                )
                fail_job(conn, job_id, "splitting")
                self.assertEqual(_infer_resume_stage(conn, get_job(conn, job_id)), "splitting")
            finally:
                conn.close()

    def test_tv_with_completed_splits_resumes_at_renaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(str(Path(tmp) / "a.db"))
            try:
                job_id = self._job(conn, media_type="tv")
                rip_id = add_rip_title(conn, job_id)
                select_media(conn, job_id, "tv")
                conn.execute(
                    "INSERT INTO episode_mappings (job_id, rip_title_id, season_number, episode_start, needs_split) "
                    "VALUES (?,?,?,?,1)",
                    (job_id, rip_id, 1, 1),
                )
                conn.execute(
                    "INSERT INTO split_plans (job_id, source_file, segment_index, status) VALUES (?,?,?,?)",
                    (job_id, "t00.mkv", 0, "done"),
                )
                fail_job(conn, job_id, "splitting")
                self.assertEqual(_infer_resume_stage(conn, get_job(conn, job_id)), "renaming")
            finally:
                conn.close()


class ResumeErroredJobTests(unittest.TestCase):
    def test_resume_actually_retries_the_copy(self) -> None:
        """The reported bug: Resume on an errored job did nothing at all."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="DISC", media_type="movie")
                add_rip_title(conn, job_id)
                select_media(conn, job_id, "movie")
                conn.execute(
                    "INSERT INTO outputs (job_id, local_path, transfer_status) VALUES (?,?,?)",
                    (job_id, str(root / "movie.mkv"), "pending"),
                )
                fail_job(conn, job_id, "copying")

                with patch(
                    "autorippr.pipeline.transfer_job_outputs",
                    return_value={"job_id": job_id, "copied": [{"output_id": 1}], "errors": []},
                ) as transfer:
                    result = run_pipeline_for_job(conn, cfg, job_id)

                transfer.assert_called_once()
                self.assertEqual(result["status"], "done")
                self.assertEqual(get_job(conn, job_id)["status"], "done")
            finally:
                conn.close()

    def test_resume_does_not_re_rip_a_completed_disc(self) -> None:
        """A failed copy must never cost another rip."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="DISC", media_type="movie")
                add_rip_title(conn, job_id)
                select_media(conn, job_id, "movie")
                conn.execute(
                    "INSERT INTO outputs (job_id, local_path, transfer_status) VALUES (?,?,?)",
                    (job_id, str(root / "movie.mkv"), "pending"),
                )
                fail_job(conn, job_id, "copying")

                with patch("autorippr.pipeline.execute_rip_job") as rip, patch(
                    "autorippr.pipeline.transfer_job_outputs",
                    return_value={"job_id": job_id, "copied": [], "errors": []},
                ):
                    run_pipeline_for_job(conn, cfg, job_id)

                rip.assert_not_called()
            finally:
                conn.close()

    def test_resume_clears_the_previous_error_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="DISC", media_type="movie")
                add_rip_title(conn, job_id)
                select_media(conn, job_id, "movie")
                conn.execute(
                    "INSERT INTO outputs (job_id, local_path, transfer_status) VALUES (?,?,?)",
                    (job_id, str(root / "movie.mkv"), "pending"),
                )
                fail_job(conn, job_id, "copying")
                self.assertIsNotNone(get_job(conn, job_id)["error_message"])

                with patch(
                    "autorippr.pipeline.transfer_job_outputs",
                    return_value={"job_id": job_id, "copied": [], "errors": []},
                ):
                    run_pipeline_for_job(conn, cfg, job_id)

                self.assertIsNone(get_job(conn, job_id)["error_message"])
            finally:
                conn.close()

    def test_repeated_failure_returns_to_error(self) -> None:
        """Retrying while the NAS is still down must fail cleanly, not loop."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="DISC", media_type="movie")
                add_rip_title(conn, job_id)
                select_media(conn, job_id, "movie")
                conn.execute(
                    "INSERT INTO outputs (job_id, local_path, transfer_status) VALUES (?,?,?)",
                    (job_id, str(root / "movie.mkv"), "pending"),
                )
                fail_job(conn, job_id, "copying")

                with patch(
                    "autorippr.pipeline.transfer_job_outputs",
                    return_value={
                        "job_id": job_id,
                        "copied": [],
                        "errors": [{"output_id": 1, "error": "nas_unavailable"}],
                    },
                ):
                    result = run_pipeline_for_job(conn, cfg, job_id)

                self.assertEqual(result["status"], "error")
                job = get_job(conn, job_id)
                self.assertEqual(job["status"], "error")
                self.assertIn("nas_unavailable", job["error_message"])
            finally:
                conn.close()


class NasPreflightTests(unittest.TestCase):
    """Ripping is local and must never wait on, or be blocked by, the NAS."""

    def _run_probe(self, side_effect):
        import autorippr.pipeline as pipeline

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="DISC", media_type="movie")
                conn.commit()
                with patch.object(pipeline, "ensure_nas_available", side_effect=side_effect):
                    started = time.monotonic()
                    pipeline._warn_if_nas_unreachable(conn, cfg, job_id)
                    elapsed = time.monotonic() - started
                warnings = [
                    r["message"]
                    for r in conn.execute(
                        "SELECT message FROM job_logs WHERE job_id = ? AND level = 'WARNING'",
                        (job_id,),
                    ).fetchall()
                ]
                return elapsed, warnings
            finally:
                conn.close()

    def test_unreachable_nas_warns_without_raising(self) -> None:
        from autorippr.transfer import TransferError

        elapsed, warnings = self._run_probe(TransferError("NAS root Y:\ is not reachable."))
        self.assertEqual(len(warnings), 1)
        self.assertIn("rip will continue", warnings[0])

    def test_reachable_nas_says_nothing(self) -> None:
        elapsed, warnings = self._run_probe(None)
        self.assertEqual(warnings, [])

    def test_a_hanging_probe_does_not_stall_the_rip(self) -> None:
        """A dead SMB share blocks for tens of seconds; the rip must not wait."""
        import autorippr.pipeline as pipeline

        def hang(_cfg):
            time.sleep(30)

        elapsed, warnings = self._run_probe(hang)
        self.assertLess(elapsed, pipeline.NAS_PREFLIGHT_TIMEOUT_SECONDS + 1.5)
        # An inconclusive probe says nothing rather than guessing.
        self.assertEqual(warnings, [])

    def test_unexpected_probe_error_is_swallowed(self) -> None:
        elapsed, warnings = self._run_probe(OSError("something odd"))
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()

"""
Tests for the path a TV job actually takes.

Movies and TV leave `queued` in opposite directions. A movie rips first and is
identified from what came off the disc; TV is identified first, so that mapping
already knows the episode list when the rip lands. pipeline.py has branched
this way since the first commit, and _advance_after_identify has always sent an
unripped TV job on to `ripping`.

None of those transitions were permitted by the state machine. Every TV job
started from `queued` died on its first step with InvalidTransitionError, and
the database bears it out: 185 jobs reached `ripping` from `queued`, and not
one ever reached `identifying`. Nothing tested the path, so nothing said so.

The three edges below are the ones the pipeline requests and the table refused.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.config import AppConfig  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.state import can_transition, create_job, get_job  # noqa: E402


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


class TvTransitionTests(unittest.TestCase):
    """Every edge the pipeline asks for has to exist in the table."""

    def test_tv_is_identified_before_it_rips(self) -> None:
        self.assertTrue(can_transition("queued", "identifying"))

    def test_an_identified_tv_job_goes_on_to_rip(self) -> None:
        # _advance_after_identify returns "ripping" for a TV job with no rip
        # titles yet, and logs that it is starting the rip with season context.
        self.assertTrue(can_transition("identifying", "ripping"))

    def test_a_pre_identified_rip_goes_straight_to_mapping(self) -> None:
        # _advance_after_rip returns "mapping" when the show is already known,
        # skipping the identify step it no longer needs.
        self.assertTrue(can_transition("ripping", "mapping"))

    def test_the_movie_path_is_untouched(self) -> None:
        self.assertTrue(can_transition("queued", "ripping"))
        self.assertTrue(can_transition("ripping", "identifying"))
        self.assertTrue(can_transition("identifying", "renaming"))

    def test_nonsense_is_still_refused(self) -> None:
        # Widening the table must not turn it into a rubber stamp.
        self.assertFalse(can_transition("queued", "done"))
        self.assertFalse(can_transition("queued", "copying"))
        self.assertFalse(can_transition("done", "ripping"))
        self.assertFalse(can_transition("copying", "mapping"))

    def test_every_stage_can_still_fail(self) -> None:
        for stage in ("queued", "ripping", "identifying", "mapping", "splitting", "renaming", "copying"):
            self.assertTrue(can_transition(stage, "error"), stage)


class TvPipelineWalkTests(unittest.TestCase):
    """
    The whole path, not just the edges.

    Testing the transitions alone would not have caught this: each one was
    individually plausible, and it was only running a TV job start to finish
    that showed the table and the pipeline disagreeing.
    """

    def test_a_tv_job_reaches_mapping_without_a_human(self) -> None:
        from autorippr import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(
                    conn, disc_label="SHOW_S1_D1", media_type="tv", disc_scope="full_season", season_number=1
                )
                conn.execute(
                    "INSERT INTO job_selected_media (job_id, media_type, tmdb_id, title, season_number, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (job_id, "tv", 3934, "Show", 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                )
                conn.execute(
                    "INSERT INTO tmdb_candidates (job_id, tmdb_id, media_type, title, year, score, "
                    "score_breakdown_json, selected, manual_override) VALUES (?,?,?,?,?,?,?,1,1)",
                    (job_id, 3934, "tv", "Show", 2006, 1.0, "{}"),
                )
                conn.commit()

                with patch.object(pipeline, "map_job_episodes", return_value={"needs_review": True, "mappings": []}), \
                     patch.object(pipeline, "_warn_if_nas_unreachable"), \
                     patch.object(pipeline, "eject_drive", return_value=True), \
                     patch.object(pipeline, "analyze_dvd_menu"):
                    result = pipeline.run_pipeline_for_job(conn, cfg, job_id, mock_rip=True)

                self.assertEqual(result["status"], "mapping")
                self.assertEqual(get_job(conn, job_id)["status"], "mapping")
            finally:
                conn.close()

    def test_it_does_not_stall_in_queued(self) -> None:
        # The observed symptom: the job errored on its very first step, before
        # the disc was touched.
        from autorippr import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="SHOW", media_type="tv", season_number=1)
                with patch.object(pipeline, "identify_job_with_tmdb",
                                  return_value={"needs_review": True, "selected": None, "candidates": []}), \
                     patch.object(pipeline, "analyze_dvd_menu"), \
                     patch.object(pipeline, "_warn_if_nas_unreachable"), \
                     patch.object(pipeline, "release_disc"):
                    pipeline.run_pipeline_for_job(conn, cfg, job_id, mock_rip=True)
                self.assertNotEqual(get_job(conn, job_id)["status"], "error")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

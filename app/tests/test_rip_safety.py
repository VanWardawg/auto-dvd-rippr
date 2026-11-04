"""
Guards against a rip finishing with the wrong file.

On 2026-10-29 a single-episode disc ("Nephi and the Brass Plates") went to
the NAS as a 3-minute extra. The drive was slow, the stall warning fired
during MakeMKV's analysis, and Resume started a second pipeline beside the
first. Read errors then hid the 24-minute title from MakeMKV's re-enumeration
at rip time, title 0 became the extra, MakeMKV reported success, and the job
went to done. Each test here fails without one of the fixes.
"""

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

import main as cli_main  # noqa: E402
from autorippr import pipeline, rip  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.joblock import job_run_lock  # noqa: E402
from autorippr.state import create_job  # noqa: E402
from test_single_episode import build_config  # noqa: E402


# What the scan before the rip reported for that disc.
SCAN = {
    0: {"fields": {"9": "0:24:43"}},
    1: {"fields": {"9": "0:03:10"}},
    2: {"fields": {"9": "0:02:06"}},
}


def ripped(title_id: int, seconds: float):
    return rip.RipTitleMetadata(
        title_id=title_id,
        duration_seconds=seconds,
        chapter_count=0,
        source_file=f"title_t{title_id:02d}.mkv",
        raw_metadata={},
    )


class RippedLengthTests(unittest.TestCase):
    def test_a_renumbered_title_is_caught(self) -> None:
        # The real failure: title_t00.mkv was the 3:10 extra, not the episode.
        problems = rip._titles_not_matching_the_scan([ripped(0, 190.528)], SCAN)
        self.assertEqual(len(problems), 1)
        self.assertIn("24:43", problems[0])
        self.assertIn("3:11", problems[0])

    def test_the_older_any_length_check_lets_it_through(self) -> None:
        # Why a new check was needed: the extra *is* a length on the disc.
        self.assertTrue(rip._durations_match_the_disc([ripped(0, 190.528)], SCAN))

    def test_a_correct_rip_passes(self) -> None:
        self.assertEqual(rip._titles_not_matching_the_scan([ripped(0, 1482.9)], SCAN), [])

    def test_rounding_noise_passes(self) -> None:
        self.assertEqual(rip._titles_not_matching_the_scan([ripped(0, 1478.0)], SCAN), [])

    def test_no_scan_means_no_verdict(self) -> None:
        self.assertEqual(rip._titles_not_matching_the_scan([ripped(0, 190.5)], {}), [])

    def test_recovery_refuses_the_wrong_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="NEPHI", media_type="tv")
                job_root = root / "jobs" / job_id
                (job_root / "rip_output").mkdir(parents=True)
                (job_root / "logs").mkdir(parents=True)
                (job_root / "rip_output" / "title_t00.mkv").write_bytes(b"x")
                (job_root / "logs" / "makemkv.log").write_text("Copy complete. 1 titles saved.", encoding="utf-8")
                (job_root / "logs" / "makemkv_disc_info.log").write_text("", encoding="utf-8")
                with patch.object(rip, "_parse_makemkv_info_output", return_value=SCAN), patch.object(
                    rip, "_build_title_metadata", return_value=[ripped(0, 190.528)]
                ):
                    self.assertIsNone(rip.recover_completed_rip(conn, cfg, job_id))
            finally:
                conn.close()


class TitleIdFromFilenameTests(unittest.TestCase):
    def test_digits_in_the_disc_title_are_not_the_title_number(self) -> None:
        # Read as title 2 from "Part 2", which failed every Deathly Hallows rip.
        self.assertEqual(rip._extract_title_id("Harry Potter and the Deathly Hallows Part 2_t00", 9), 0)
        self.assertEqual(rip._extract_title_id("Harry Potter and the Deathly Hallows Part 1_t03", 9), 3)

    def test_plain_makemkv_names_still_parse(self) -> None:
        self.assertEqual(rip._extract_title_id("title_t01", 9), 1)
        self.assertEqual(rip._extract_title_id("Harry Potter and the Half-Blood Prince_t01", 9), 1)


class OnePipelinePerJobTests(unittest.TestCase):
    def test_a_second_run_does_not_start_while_the_first_holds_the_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="NEPHI", media_type="tv")
                with patch.object(pipeline, "_run_pipeline_for_job") as inner:
                    with job_run_lock(cfg.staging_root, job_id):
                        result = pipeline.run_pipeline_for_job(conn, cfg, job_id)
                    inner.assert_not_called()
                    self.assertTrue(result["already_running"])

                    # And once the first run lets go, the next one proceeds.
                    inner.return_value = {"status": "done"}
                    self.assertEqual(pipeline.run_pipeline_for_job(conn, cfg, job_id), {"status": "done"})
                messages = [r["message"] for r in conn.execute("SELECT message FROM job_logs WHERE job_id = ?", (job_id,))]
                self.assertTrue(any("still running" in m for m in messages))
            finally:
                conn.close()


class StallWarningTests(unittest.TestCase):
    def review(self, *, detail: str, still_for: float):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            job_id = "job-1"
            (Path(tmp) / "jobs" / job_id / "logs").mkdir(parents=True)
            return cli_main._build_review_state(
                cfg=SimpleNamespace(staging_root=tmp),
                job={"id": job_id, "status": "ripping", "media_type": "tv", "movie_mode": "single"},
                logs=[],
                progress_row={
                    "stage": "ripping",
                    "detail": detail,
                    "updated_at": now.isoformat(),
                    "last_advance_at": (now - timedelta(seconds=still_for)).isoformat(),
                },
                selected_media=None,
                selected_movies=[],
                tmdb_rows=[],
                mapping_rows=[],
                rip_title_rows=[],
                bundle_association=None,
                tmdb_threshold=0.75,
            )["rip"]

    def test_a_slow_analysis_phase_is_not_called_stalled(self) -> None:
        self.assertFalse(self.review(detail="Processing title sets", still_for=120)["needed"])

    def test_a_long_analysis_phase_still_is(self) -> None:
        self.assertTrue(self.review(detail="Processing title sets", still_for=400)["needed"])

    def test_saving_keeps_the_short_threshold(self) -> None:
        self.assertTrue(self.review(detail="Saving to MKV file - 32 MB written", still_for=120)["needed"])


if __name__ == "__main__":
    unittest.main()

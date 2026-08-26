"""
Tests for splitting combined episodes.

When a disc puts two episodes in one title, this module decides where to cut.
Get it wrong and the output is plausible but useless: an episode that starts
half a minute late, or one that runs into the next. Nothing downstream
notices, because the file is valid and correctly named. It had no tests.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.config import AppConfig  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.splitter import (  # noqa: E402
    SplitError,
    _build_ffmpeg_split_cmd,
    _extract_chapter_bounds,
    plan_splits_for_job,
    set_manual_split_timestamps,
)
from autorippr.state import create_job  # noqa: E402


def build_config(root: Path) -> AppConfig:
    return AppConfig(
        tmdb_api_key="k",
        makemkv_path="x",
        ffmpeg_path=str(root / "ffmpeg.exe"),
        ffprobe_path=str(root / "ffprobe.exe"),
        staging_root=str(root),
        nas_root=str(root / "nas"),
        db_path=str(root / "autorippr.db"),
        log_path=str(root / "autorippr.log"),
    )


def chapters(*bounds):
    """ffprobe-shaped chapter metadata."""
    return json.dumps(
        {"chapters": [{"start_time": str(s), "end_time": str(e)} for s, e in bounds]}
    )


class ChapterBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = open_db(str(Path(self._tmp.name) / "a.db"))
        self.job_id = create_job(self.conn, disc_label="D", media_type="tv")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _title(self, raw_json):
        self.conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, duration_seconds, chapter_count, source_file, raw_metadata_json) "
            "VALUES (?,?,?,?,?,?)",
            (self.job_id, 0, 2640.0, 12, "t00.mkv", raw_json),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]

    def test_reads_chapter_times(self) -> None:
        rid = self._title(chapters((0, 60), (60, 130.5)))
        self.assertEqual(_extract_chapter_bounds(self.conn, rid), [(0.0, 60.0), (60.0, 130.5)])

    def test_no_metadata_yields_nothing(self) -> None:
        rid = self._title(None)
        self.assertEqual(_extract_chapter_bounds(self.conn, rid), [])

    def test_malformed_json_does_not_raise(self) -> None:
        rid = self._title("{not json")
        self.assertEqual(_extract_chapter_bounds(self.conn, rid), [])

    def test_chapters_with_unusable_times_are_skipped(self) -> None:
        raw = json.dumps({"chapters": [
            {"start_time": "0", "end_time": "60"},
            {"start_time": "bad", "end_time": "90"},
            {"start_time": "90", "end_time": "150"},
        ]})
        rid = self._title(raw)
        self.assertEqual(_extract_chapter_bounds(self.conn, rid), [(0.0, 60.0), (90.0, 150.0)])

    def test_missing_rip_title_is_not_an_error(self) -> None:
        self.assertEqual(_extract_chapter_bounds(self.conn, None), [])


class PlanSplitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.conn = open_db(str(self.root / "a.db"))
        self.job_id = create_job(self.conn, disc_label="D", media_type="tv")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _mapping(self, start, end, *, duration=2640.0, chapter_count=0, raw=None, source="t00.mkv"):
        self.conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, duration_seconds, chapter_count, source_file, raw_metadata_json) "
            "VALUES (?,?,?,?,?,?)",
            (self.job_id, 0, duration, chapter_count, source, raw),
        )
        rid = self.conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        self.conn.execute(
            "INSERT INTO episode_mappings (job_id, rip_title_id, season_number, episode_start, episode_end, needs_split) "
            "VALUES (?,?,?,?,?,1)",
            (self.job_id, rid, 1, start, end),
        )
        self.conn.commit()

    def _plans(self):
        return self.conn.execute(
            "SELECT segment_index, start_seconds, end_seconds, chapter_start, chapter_end, status "
            "FROM split_plans WHERE job_id = ? ORDER BY segment_index",
            (self.job_id,),
        ).fetchall()

    def test_two_episodes_produce_two_segments(self) -> None:
        self._mapping(1, 2)
        result = plan_splits_for_job(self.conn, self.job_id)
        self.assertEqual(result["split_plan_count"], 2)
        self.assertEqual([p["segment_index"] for p in self._plans()], [1, 2])

    def test_duration_fallback_splits_evenly_and_covers_the_whole_title(self) -> None:
        """No chapter data, so the only sane cut is down the middle."""
        self._mapping(1, 2, duration=2640.0)
        plan_splits_for_job(self.conn, self.job_id)
        plans = self._plans()
        self.assertAlmostEqual(plans[0]["start_seconds"], 0.0)
        self.assertAlmostEqual(plans[0]["end_seconds"], 1320.0)
        self.assertAlmostEqual(plans[1]["start_seconds"], 1320.0)
        # The last segment must run to the end, not to a rounded boundary,
        # or the tail of the final episode is silently dropped.
        self.assertAlmostEqual(plans[1]["end_seconds"], 2640.0)

    def test_chapter_boundaries_are_preferred_over_even_division(self) -> None:
        """Chapters mark the real episode break; even division only approximates it."""
        raw = chapters(*[(i * 100.0, (i + 1) * 100.0) for i in range(12)])
        self._mapping(1, 2, duration=1200.0, chapter_count=12, raw=raw)
        plan_splits_for_job(self.conn, self.job_id)
        plans = self._plans()
        self.assertEqual(plans[0]["chapter_start"], 1)
        self.assertEqual(plans[0]["chapter_end"], 6)
        self.assertEqual(plans[1]["chapter_start"], 7)
        self.assertEqual(plans[1]["chapter_end"], 12)
        self.assertAlmostEqual(plans[0]["start_seconds"], 0.0)
        self.assertAlmostEqual(plans[0]["end_seconds"], 600.0)
        self.assertAlmostEqual(plans[1]["end_seconds"], 1200.0)

    def test_final_segment_takes_every_remaining_chapter(self) -> None:
        """13 chapters across 2 episodes must not leave chapter 13 unassigned."""
        raw = chapters(*[(i * 100.0, (i + 1) * 100.0) for i in range(13)])
        self._mapping(1, 2, duration=1300.0, chapter_count=13, raw=raw)
        plan_splits_for_job(self.conn, self.job_id)
        plans = self._plans()
        self.assertEqual(plans[1]["chapter_end"], 13)

    def test_three_episodes_in_one_title(self) -> None:
        self._mapping(4, 6, duration=3600.0)
        plan_splits_for_job(self.conn, self.job_id)
        plans = self._plans()
        self.assertEqual(len(plans), 3)
        self.assertAlmostEqual(plans[2]["end_seconds"], 3600.0)

    def test_replanning_replaces_the_previous_plans(self) -> None:
        """Re-running must not leave stale segments behind to be split twice."""
        self._mapping(1, 2)
        plan_splits_for_job(self.conn, self.job_id)
        plan_splits_for_job(self.conn, self.job_id)
        self.assertEqual(len(self._plans()), 2)

    def test_mapping_without_a_source_file_is_skipped(self) -> None:
        self._mapping(1, 2, source=None)
        result = plan_splits_for_job(self.conn, self.job_id)
        self.assertEqual(result["split_plan_count"], 0)

    def test_mappings_not_flagged_for_split_are_ignored(self) -> None:
        self.conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
            (self.job_id, 0, 1320.0, "t00.mkv"),
        )
        rid = self.conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        self.conn.execute(
            "INSERT INTO episode_mappings (job_id, rip_title_id, season_number, episode_start, episode_end, needs_split) "
            "VALUES (?,?,?,?,?,0)",
            (self.job_id, rid, 1, 1, 1),
        )
        self.conn.commit()
        self.assertEqual(plan_splits_for_job(self.conn, self.job_id)["split_plan_count"], 0)

    def test_manual_timestamps_override_and_clear_chapters(self) -> None:
        self._mapping(1, 2, duration=1200.0, chapter_count=12,
                      raw=chapters(*[(i * 100.0, (i + 1) * 100.0) for i in range(12)]))
        plan_splits_for_job(self.conn, self.job_id)
        plan_id = self.conn.execute(
            "SELECT id FROM split_plans WHERE job_id = ? ORDER BY segment_index LIMIT 1", (self.job_id,)
        ).fetchone()["id"]

        set_manual_split_timestamps(self.conn, plan_id, 12.5, 615.0)

        row = self.conn.execute(
            "SELECT start_seconds, end_seconds, chapter_start, chapter_end, status FROM split_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        self.assertAlmostEqual(row["start_seconds"], 12.5)
        self.assertAlmostEqual(row["end_seconds"], 615.0)
        # Chapter hints must go, or a later run could reapply them over the
        # manual correction.
        self.assertIsNone(row["chapter_start"])
        self.assertIsNone(row["chapter_end"])
        self.assertEqual(row["status"], "pending")

    def test_manual_timestamps_on_a_missing_plan_raise(self) -> None:
        with self.assertRaises(SplitError):
            set_manual_split_timestamps(self.conn, 999999, 0.0, 10.0)


class FfmpegCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ffmpeg = self.root / "ffmpeg.exe"
        self.ffmpeg.write_bytes(b"")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_builds_a_stream_copy_with_both_bounds(self) -> None:
        cmd = _build_ffmpeg_split_cmd(
            str(self.ffmpeg), Path("in.mkv"), Path("out.mkv"), 10.0, 20.0, None, None
        )
        self.assertIn("-ss", cmd)
        self.assertIn("-to", cmd)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "10.0")
        self.assertEqual(cmd[cmd.index("-to") + 1], "20.0")
        # Re-encoding a 20 minute episode would take minutes; stream copy is
        # the whole point.
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertEqual(cmd[-1], "out.mkv")

    def test_refuses_a_segment_with_no_boundaries(self) -> None:
        """Without bounds ffmpeg would copy the entire title over each episode."""
        with self.assertRaises(SplitError) as ctx:
            _build_ffmpeg_split_cmd(str(self.ffmpeg), Path("in.mkv"), Path("out.mkv"), None, None, None, None)
        self.assertIn("timing", str(ctx.exception).lower())

    def test_start_only_is_allowed(self) -> None:
        cmd = _build_ffmpeg_split_cmd(str(self.ffmpeg), Path("in.mkv"), Path("out.mkv"), 5.0, None, None, None)
        self.assertIn("-ss", cmd)
        self.assertNotIn("-to", cmd)

    def test_missing_ffmpeg_is_reported_clearly(self) -> None:
        with self.assertRaises(SplitError) as ctx:
            _build_ffmpeg_split_cmd(str(self.root / "nope.exe"), Path("in.mkv"), Path("out.mkv"), 0.0, 1.0, None, None)
        self.assertIn("ffmpeg not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

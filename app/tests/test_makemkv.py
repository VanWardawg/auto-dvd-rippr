"""
Tests for MakeMKV robot-output parsing and title selection.

These guard the decisions that determine how much of a disc gets ripped, which
is the difference between a 20-minute and a 60-minute disc.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import autorippr.rip as rip  # noqa: E402
from autorippr.config import AppConfig  # noqa: E402
from autorippr.makemkv import (  # noqa: E402
    TitleCandidate,
    build_title_candidates,
    overall_fraction,
    parse_duration,
    parse_progress_line,
    select_titles,
)


def title(title_id: int, minutes: float, *, size_gb: float = 1.0, name: str = "") -> TitleCandidate:
    return TitleCandidate(
        title_id=title_id,
        duration_seconds=minutes * 60.0,
        size_bytes=int(size_gb * 1024**3),
        chapter_count=6,
        segment_count=1,
        name=name or f"Title {title_id}",
    )


class ParseDurationTests(unittest.TestCase):
    def test_parses_hours_minutes_seconds(self) -> None:
        self.assertEqual(parse_duration("1:23:45"), 5025.0)

    def test_parses_minutes_seconds(self) -> None:
        self.assertEqual(parse_duration("23:45"), 1425.0)

    def test_returns_zero_for_garbage(self) -> None:
        self.assertEqual(parse_duration("not a duration"), 0.0)
        self.assertEqual(parse_duration(""), 0.0)


class ProgressParsingTests(unittest.TestCase):
    def test_parses_progress_values(self) -> None:
        event = parse_progress_line("PRGV:16384,32768,65536")
        assert event is not None
        self.assertEqual(event.kind, "values")
        self.assertEqual(event.current, 16384)
        self.assertEqual(event.total, 32768)
        self.assertAlmostEqual(overall_fraction(event), 0.5)

    def test_parses_current_operation_name(self) -> None:
        event = parse_progress_line('PRGC:5057,0,"Analyzing seamless segments"')
        assert event is not None
        self.assertEqual(event.kind, "current_op")
        self.assertEqual(event.text, "Analyzing seamless segments")

    def test_parses_message_with_embedded_commas(self) -> None:
        event = parse_progress_line(
            'MSG:3007,0,2,"File 00:01, chapter 2 added","%1 added","x"'
        )
        assert event is not None
        self.assertEqual(event.kind, "message")
        self.assertEqual(event.text, "File 00:01, chapter 2 added")

    def test_ignores_unrelated_lines(self) -> None:
        self.assertIsNone(parse_progress_line("Saving 3 titles into directory C:\\out"))
        self.assertIsNone(parse_progress_line(""))

    def test_survives_malformed_progress_line(self) -> None:
        self.assertIsNone(parse_progress_line("PRGV:only,two"))
        self.assertIsNone(parse_progress_line("PRGV:a,b,c"))

    def test_zero_maximum_does_not_divide_by_zero(self) -> None:
        event = parse_progress_line("PRGV:1,2,0")
        assert event is not None
        self.assertEqual(event.maximum, 65536)


class BuildTitleCandidatesTests(unittest.TestCase):
    def test_builds_from_tinfo_fields(self) -> None:
        per_title = {
            0: {
                "fields": {"9": "1:30:00", "8": "12", "11": "4294967296", "2": "Feature"},
                "display_name": "Feature",
            }
        }
        candidates = build_title_candidates(per_title)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].duration_seconds, 5400.0)
        self.assertEqual(candidates[0].chapter_count, 12)
        self.assertEqual(candidates[0].size_bytes, 4294967296)

    def test_missing_fields_do_not_raise(self) -> None:
        candidates = build_title_candidates({3: {"fields": {}}})
        self.assertEqual(candidates[0].title_id, 3)
        self.assertEqual(candidates[0].duration_seconds, 0.0)


class MovieSelectionTests(unittest.TestCase):
    def test_picks_the_feature_and_skips_extras(self) -> None:
        candidates = [
            title(0, 118, name="Feature"),
            title(1, 3, name="Studio logo"),
            title(2, 8, name="Trailer"),
            title(3, 12, name="Making of"),
        ]
        selection = select_titles(candidates, media_type="movie")
        self.assertEqual(selection.title_ids, [0])
        self.assertEqual(len(selection.skipped), 3)
        self.assertFalse(selection.is_everything)

    def test_double_feature_keeps_both_features(self) -> None:
        candidates = [
            title(0, 95, name="Feature A"),
            title(1, 88, name="Feature B"),
            title(2, 4, name="Trailer"),
        ]
        selection = select_titles(candidates, media_type="movie", movie_mode="double_feature")
        self.assertEqual(selection.title_ids, [0, 1])

    def test_bluray_playlist_duplicates_collapse_to_largest(self) -> None:
        """Playlist obfuscation lists the same feature many times."""
        candidates = [
            title(0, 120, size_gb=8.0, name="Playlist 800"),
            title(1, 120, size_gb=30.0, name="Playlist 801"),
            title(2, 120, size_gb=12.0, name="Playlist 802"),
        ]
        selection = select_titles(candidates, media_type="movie")
        self.assertEqual(selection.title_ids, [1])

    def test_single_title_disc_selects_everything(self) -> None:
        selection = select_titles([title(0, 100)], media_type="movie")
        self.assertTrue(selection.is_everything)

    def test_missing_durations_fall_back_to_all(self) -> None:
        candidates = [title(0, 0), title(1, 0)]
        selection = select_titles(candidates, media_type="movie")
        self.assertTrue(selection.is_everything)
        self.assertEqual(selection.title_ids, [0, 1])


class TvSelectionTests(unittest.TestCase):
    def test_keeps_episodes_and_drops_play_all(self) -> None:
        candidates = [
            title(0, 44, name="Episode 1"),
            title(1, 44, name="Episode 2"),
            title(2, 44, name="Episode 3"),
            title(3, 132, name="Play All"),
            title(4, 2, name="Logo"),
        ]
        selection = select_titles(candidates, media_type="tv")
        self.assertEqual(selection.title_ids, [0, 1, 2])
        self.assertTrue(any("play-all" in s for s in selection.skipped))

    def test_keeps_combined_episode_titles(self) -> None:
        """Two episodes in one title must be kept -- the splitter handles them."""
        candidates = [
            title(0, 44, name="Episode 1"),
            title(1, 95, name="Episodes 2-3 combined"),
            title(2, 3, name="Trailer"),
        ]
        selection = select_titles(
            candidates, media_type="tv", min_episode_minutes=10.0, max_episode_minutes=60.0
        )
        self.assertEqual(selection.title_ids, [0, 1])

    def test_all_episodes_means_rip_everything(self) -> None:
        candidates = [title(0, 44), title(1, 44)]
        selection = select_titles(candidates, media_type="tv")
        self.assertTrue(selection.is_everything)

    def test_nothing_matching_window_falls_back_to_all(self) -> None:
        """An unusual disc must not silently produce an empty rip."""
        candidates = [title(0, 5), title(1, 6)]
        selection = select_titles(
            candidates, media_type="tv", min_episode_minutes=20.0, max_episode_minutes=60.0
        )
        self.assertTrue(selection.is_everything)
        self.assertEqual(selection.title_ids, [0, 1])

    def test_empty_disc_returns_no_titles(self) -> None:
        selection = select_titles([], media_type="tv")
        self.assertEqual(selection.title_ids, [])


class SpaceGuardTests(unittest.TestCase):
    """
    MakeMKV reports title sizes during the scan, so a rip that cannot fit is
    knowable before it starts. Finding out an hour in leaves a part-written
    file and a disc to rip over again.
    """

    def _cfg(self, staging: str) -> AppConfig:
        return AppConfig(
            tmdb_api_key="k", makemkv_path="x", ffmpeg_path="x", ffprobe_path="x",
            staging_root=staging, nas_root=staging,
            db_path=staging + "/a.db", log_path=staging + "/a.log",
        )

    def _disc(self, size_bytes: int):
        return {0: {"fields": {"9": "1:30:00", "11": str(size_bytes)}}}

    def test_refuses_a_rip_that_cannot_fit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            usage = type("U", (), {"free": 1 * 1024 ** 3, "total": 100, "used": 1})
            with patch("autorippr.rip.shutil.disk_usage", return_value=usage):
                with self.assertRaises(rip.RipError) as ctx:
                    rip._ensure_space_for_rip(None, cfg, "j", self._disc(8 * 1024 ** 3), None)
            message = str(ctx.exception)
            self.assertIn("Not enough space", message)
            self.assertIn("GB is free", message)

    def test_allows_a_rip_with_room_to_spare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            usage = type("U", (), {"free": 200 * 1024 ** 3, "total": 500, "used": 1})
            with patch("autorippr.rip.shutil.disk_usage", return_value=usage):
                rip._ensure_space_for_rip(None, cfg, "j", self._disc(8 * 1024 ** 3), None)

    def test_only_counts_the_titles_being_ripped(self) -> None:
        """Selecting one feature must not be blocked by the extras' size."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            disc = {
                0: {"fields": {"9": "1:30:00", "11": str(4 * 1024 ** 3)}},
                1: {"fields": {"9": "0:05:00", "11": str(40 * 1024 ** 3)}},
            }
            usage = type("U", (), {"free": 15 * 1024 ** 3, "total": 100, "used": 1})
            with patch("autorippr.rip.shutil.disk_usage", return_value=usage):
                rip._ensure_space_for_rip(None, cfg, "j", disc, [0])
                with self.assertRaises(rip.RipError):
                    rip._ensure_space_for_rip(None, cfg, "j", disc, None)

    def test_unknown_free_space_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            with patch("autorippr.rip.shutil.disk_usage", side_effect=OSError("no")):
                rip._ensure_space_for_rip(None, cfg, "j", self._disc(999 * 1024 ** 3), None)

    def test_missing_disc_info_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rip._ensure_space_for_rip(None, self._cfg(tmp), "j", {}, None)


if __name__ == "__main__":
    unittest.main()

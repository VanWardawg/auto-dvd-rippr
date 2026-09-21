"""
Tests for the verification pass behind the positional mapping fast path.

The in-order fast path assigns ripped titles to TMDB episodes purely by disc
position, and until this pass it consulted no evidence at all. The Australian
"Bluey Season 3 First Half" DVD (job 5e2cdcc7-24b2-4c6e-9f6e-e2ded3fa019c)
plays in a custom authoring order matching neither TMDB nor the published disc
listing: all 26 files mapped confidently by position, 21 were wrong, and the
mistake was only caught by a human after the files had reached the NAS.

The rips carried the answer the whole time -- the dvd_subtitle track captions
the spoken "This episode of Bluey is called X" announcement in the first two
minutes -- so verification OCRs each fast-path title's opening frames and
compares what the episode says it is against what position claimed. The rules
under test:

- a confident OCR match to a *different* episode contradicts the assignment:
  the job goes to review with the other episode offered as a suggestion, and
  the row's confidence drops so nothing downstream trusts it;
- OCR agreeing with position, or finding nothing at all, changes nothing --
  absence of evidence keeps the fast path exactly as it was;
- verification failing outright (no subtitle track, no tools, a crash) must
  never block a mapping that was fine before verification existed.

ffmpeg and tesseract stay behind the _collect_early_identity_text seam, so
everything here runs without media or external tools.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr import mapper  # noqa: E402
from autorippr.config import AppConfig  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.mapper import (  # noqa: E402
    EpisodeTarget,
    _verify_positional_assignments,
)
from autorippr.state import create_job  # noqa: E402


def targets(*titles):
    return [
        EpisodeTarget(episode_number=i + 1, title=t, tmdb_episode_id=100 + i + 1)
        for i, t in enumerate(titles)
    ]


class Row(dict):
    """Stands in for a sqlite3.Row, which is indexed by column name."""

    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def positional_row(rip_title_id, target):
    """A planned mapping row exactly as the in-order fast path emits it."""
    return {
        "rip_title_id": rip_title_id,
        "episode_start": target.episode_number,
        "episode_end": target.episode_number,
        "tmdb_episode_ids": [target.tmdb_episode_id],
        "episode_titles": [target.title],
        "confidence": 0.94,
        "reason": (
            "Assigned in disc order because the number of clear full-length episode files matches the "
            "requested episode count, and the remaining titles are much shorter extras/alternates."
        ),
        "needs_split": False,
        "positional_fast_path": True,
    }


def rip_rows(*source_files):
    return [Row(id=i + 1, source_file=f) for i, f in enumerate(source_files)]


class PositionalVerificationTests(unittest.TestCase):
    """The decision rules, with evidence-gathering stubbed out."""

    def verify(self, planned, rows, season_targets, texts_by_rip_id):
        """Run the pass with the OCR seam answering from a fixture dict."""

        def fake_collect(cfg, job_id, source_file, rip_title_id):
            return texts_by_rip_id.get(rip_title_id, [])

        with patch.object(mapper, "_collect_early_identity_text", side_effect=fake_collect), \
             patch.object(mapper, "append_job_log") as log:
            summary = _verify_positional_assignments(
                None, None, "job-1", planned, rows, season_targets
            )
        return summary, log

    def test_a_contradicted_title_goes_to_review_with_a_suggestion(self) -> None:
        """
        The Bluey failure in miniature: position says E1 "Perfect", but the
        title's own caption announces "Dance Mode" (E3). The assignment is not
        silently rewritten -- OCR is fallible too -- but the row stops looking
        settled and the real episode is offered as the suggestion.
        """
        season = targets("Perfect", "Whale Watching", "Dance Mode")
        row = positional_row(1, season[0])
        summary, log = self.verify(
            [row],
            rip_rows("B1_t00.mkv"),
            season,
            {1: ["this episode of bluey is called dance mode"]},
        )
        self.assertEqual(summary["contradicted"], 1)
        self.assertEqual(row["verification"]["status"], "contradicted")
        self.assertEqual(row["verification"]["suggested_episode_number"], 3)
        self.assertEqual(row["verification"]["suggested_title"], "Dance Mode")
        # The positional assignment stays in place for the human to compare.
        self.assertEqual(row["episode_start"], 1)
        self.assertLessEqual(row["confidence"], 0.40)
        self.assertIn("Dance Mode", row["reason"])
        self.assertIn("suggested assignment", row["reason"])
        disagreement = summary["disagreements"][0]
        self.assertEqual(disagreement["source_file"], "B1_t00.mkv")
        self.assertEqual(disagreement["assigned_title"], "Perfect")
        self.assertEqual(disagreement["suggested_episode_number"], 3)
        warning = [c for c in log.call_args_list if c.args[2] == "WARNING"]
        self.assertEqual(len(warning), 1)
        self.assertIn("B1_t00.mkv", warning[0].args[3])
        self.assertIn("Dance Mode", warning[0].args[3])

    def test_ocr_agreeing_with_position_confirms_the_row(self) -> None:
        season = targets("Perfect", "Whale Watching")
        row = positional_row(1, season[0])
        summary, _ = self.verify(
            [row],
            rip_rows("B1_t00.mkv"),
            season,
            {1: ["this episode of bluey is called perfect"]},
        )
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["contradicted"], 0)
        self.assertEqual(row["verification"]["status"], "confirmed")
        self.assertEqual(row["confidence"], 0.94)

    def test_no_readable_text_keeps_todays_behavior(self) -> None:
        """No subtitle track and no on-screen title is the common case for a
        lot of discs; it must leave the fast path untouched, not punish it."""
        season = targets("Perfect", "Whale Watching")
        row = positional_row(1, season[0])
        summary, log = self.verify([row], rip_rows("B1_t00.mkv"), season, {1: []})
        self.assertEqual(summary["checked"], 0)
        self.assertEqual(summary["contradicted"], 0)
        self.assertNotIn("verification", row)
        self.assertEqual(row["confidence"], 0.94)
        log.assert_not_called()

    def test_text_matching_no_episode_at_all_is_not_evidence(self) -> None:
        season = targets("Perfect", "Whale Watching")
        row = positional_row(1, season[0])
        summary, _ = self.verify(
            [row],
            rip_rows("B1_t00.mkv"),
            season,
            {1: ["mumble static 1080i copyright warning"]},
        )
        self.assertEqual(summary["checked"], 0)
        self.assertNotIn("verification", row)

    def test_a_superset_title_reading_is_ambiguity_not_contradiction(self) -> None:
        """
        OCR reading "Dance Mode" from a title assigned "Dance Mode Part Two"
        scores the sibling episode highest, but the assigned episode scores
        nearly as well -- the caption is compatible with both. That must not
        overrule the disc order.
        """
        season = targets("Dance Mode Part Two", "Dance Mode")
        row = positional_row(1, season[0])
        summary, _ = self.verify(
            [row], rip_rows("B1_t00.mkv"), season, {1: ["dance mode"]}
        )
        self.assertEqual(summary["contradicted"], 0)
        self.assertEqual(row["verification"]["status"], "inconclusive")
        self.assertEqual(row["confidence"], 0.94)

    def test_a_weak_partial_match_is_not_a_contradiction(self) -> None:
        """A couple of shared words is a hint, not a clear identification."""
        season = targets("Keepy Uppy", "Magic Xylophone Adventure")
        row = positional_row(1, season[0])
        summary, _ = self.verify(
            [row],
            rip_rows("B1_t00.mkv"),
            season,
            {1: ["magic xylophone thing today"]},
        )
        self.assertEqual(summary["contradicted"], 0)
        self.assertEqual(row["verification"]["status"], "inconclusive")
        self.assertEqual(row["confidence"], 0.94)

    def test_only_fast_path_rows_are_checked(self) -> None:
        """Menu and OCR matched rows already carry their own evidence; the
        pass exists for the rows that have none."""
        season = targets("Perfect", "Whale Watching")
        row = positional_row(1, season[0])
        del row["positional_fast_path"]
        seam = Mock(return_value=["this episode of bluey is called whale watching"])
        with patch.object(mapper, "_collect_early_identity_text", seam), \
             patch.object(mapper, "append_job_log"):
            summary = _verify_positional_assignments(
                None, None, "job-1", [row], rip_rows("B1_t00.mkv"), season
            )
        seam.assert_not_called()
        self.assertEqual(summary["checked"], 0)

    def test_a_crash_in_evidence_gathering_never_blocks_the_mapping(self) -> None:
        season = targets("Perfect", "Whale Watching")
        row = positional_row(1, season[0])
        with patch.object(
            mapper, "_collect_early_identity_text", side_effect=RuntimeError("ffmpeg exploded")
        ), patch.object(mapper, "append_job_log"):
            summary = _verify_positional_assignments(
                None, None, "job-1", [row], rip_rows("B1_t00.mkv"), season
            )
        self.assertEqual(summary["checked"], 0)
        self.assertEqual(row["confidence"], 0.94)
        self.assertNotIn("verification", row)


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


class MapJobEpisodesVerificationTests(unittest.TestCase):
    """
    The whole mapping call, the way the pipeline runs it.

    The Bluey incident was not a unit-level failure: every piece behaved as
    designed and the job still shipped 21 wrong files, because nothing between
    the fast path and the NAS ever looked at the content. These walk
    map_job_episodes end to end and check that a contradicted disc now stops
    for review instead.
    """

    def run_mapping(self, *, episodes, ocr_by_title_id, disc_scope="full_season",
                    range_start=None, range_end=None, title_count=None):
        if title_count is None:
            title_count = len(episodes) if range_start is None else (range_end - range_start + 1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(
                    conn,
                    disc_label="BLUEY_S3_FIRST_HALF",
                    media_type="tv",
                    disc_scope=disc_scope,
                    season_number=3,
                    episode_range_start=range_start,
                    episode_range_end=range_end,
                )
                conn.execute(
                    "INSERT INTO job_selected_media (job_id, media_type, tmdb_id, title, season_number, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (job_id, "tv", 82728, "Bluey", 3,
                     "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                )
                for i in range(title_count):
                    conn.execute(
                        "INSERT INTO rip_titles (job_id, title_id, duration_seconds, chapter_count, source_file, raw_metadata_json) "
                        "VALUES (?,?,?,?,?,?)",
                        (job_id, i, 9 * 60.0, 4, str(root / f"B1_t{i:02d}.mkv"), "{}"),
                    )
                conn.commit()

                def fake_collect(cfg_, job_id_, source_file, rip_title_id):
                    # rip_title ids are 1-based in insertion order; the fixture
                    # is keyed by the disc's title_id (0-based).
                    return ocr_by_title_id.get(rip_title_id - 1, [])

                with patch.object(mapper, "fetch_tmdb_tv_episodes", return_value=episodes), \
                     patch.object(mapper, "_collect_early_identity_text", side_effect=fake_collect):
                    result = mapper.map_job_episodes(conn, cfg, job_id)

                mapping_rows = conn.execute(
                    "SELECT * FROM episode_mappings WHERE job_id = ? ORDER BY rip_title_id",
                    (job_id,),
                ).fetchall()
                log_rows = conn.execute(
                    "SELECT level, message FROM job_logs WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
                return result, mapping_rows, log_rows
            finally:
                conn.close()

    @staticmethod
    def episode(number, name):
        return {"episode_number": number, "id": 500 + number, "name": name}

    def test_a_custom_authoring_order_disc_is_caught_before_it_ships(self) -> None:
        """
        The disc plays E3, E1, E2 while position hands out E1, E2, E3 -- every
        file wrong, every confidence 0.94. With the captions consulted, the
        job must stop in review, name each disagreeing title in the log, and
        persist the doubt so the review UI shows it.
        """
        episodes = [
            self.episode(1, "Perfect"),
            self.episode(2, "Whale Watching"),
            self.episode(3, "Dance Mode"),
        ]
        result, mapping_rows, log_rows = self.run_mapping(
            episodes=episodes,
            ocr_by_title_id={
                0: ["this episode of bluey is called dance mode"],
                1: ["this episode of bluey is called perfect"],
                2: ["this episode of bluey is called whale watching"],
            },
        )
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["verification"]["contradicted"], 3)
        suggested = [
            d["suggested_episode_number"] for d in result["verification"]["disagreements"]
        ]
        self.assertEqual(suggested, [3, 1, 2])
        # The stored rows keep the positional assignment but no longer claim
        # to be settled, and the reason explains the disagreement.
        for row, expected_episode in zip(mapping_rows, (1, 2, 3)):
            self.assertEqual(row["episode_start"], expected_episode)
            self.assertLess(row["confidence"], 0.85)
            self.assertIn("suggested assignment", row["reason"])
        warnings = [r["message"] for r in log_rows if r["level"] == "WARNING"]
        named = [m for m in warnings if "Positional mapping verification" in m]
        self.assertEqual(len(named), 3)
        self.assertTrue(any("B1_t00.mkv" in m and "Dance Mode" in m for m in named))

    def test_a_well_authored_disc_still_takes_the_fast_path(self) -> None:
        """Captions agreeing with position must not slow anything down or
        invent review work -- most discs really do play in order."""
        episodes = [
            self.episode(1, "Perfect"),
            self.episode(2, "Whale Watching"),
        ]
        result, mapping_rows, _ = self.run_mapping(
            episodes=episodes,
            ocr_by_title_id={
                0: ["this episode of bluey is called perfect"],
                1: ["this episode of bluey is called whale watching"],
            },
        )
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["verification"]["confirmed"], 2)
        for row in mapping_rows:
            self.assertGreaterEqual(row["confidence"], 0.85)

    def test_a_disc_with_no_captions_behaves_exactly_as_before(self) -> None:
        """Absence of evidence: no subtitle track, no on-screen text. The
        Bluey fix must not make ordinary mute discs worse."""
        episodes = [
            self.episode(1, "Perfect"),
            self.episode(2, "Whale Watching"),
        ]
        result, mapping_rows, log_rows = self.run_mapping(
            episodes=episodes,
            ocr_by_title_id={},
        )
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["verification"]["checked"], 0)
        for row in mapping_rows:
            self.assertEqual(row["confidence"], 0.94)
        self.assertFalse(
            any("Positional mapping verification" in r["message"] for r in log_rows)
        )

    def test_the_suggestion_can_point_outside_the_stated_disc_range(self) -> None:
        """
        A partial-season disc stated as E1-E3 whose files actually hold E6-E8.
        The mapping candidates are windowed to the range (plus slack), but the
        caption knows better -- so verification matches against the whole
        season and must be able to suggest an episode the window excluded.
        """
        episodes = [
            self.episode(1, "Perfect"),
            self.episode(2, "Whale Watching"),
            self.episode(3, "Dance Mode"),
            self.episode(4, "Chest"),
            self.episode(5, "Bike Shop"),
            self.episode(6, "Obstacle Course"),
            self.episode(7, "Turtle Rescue"),
            self.episode(8, "Fancy Restaurant"),
        ]
        result, _, _ = self.run_mapping(
            episodes=episodes,
            disc_scope="partial_season",
            range_start=1,
            range_end=3,
            ocr_by_title_id={
                0: ["this episode of bluey is called obstacle course"],
                1: ["this episode of bluey is called turtle rescue"],
                2: ["this episode of bluey is called fancy restaurant"],
            },
        )
        self.assertTrue(result["needs_review"])
        suggested = [
            d["suggested_episode_number"] for d in result["verification"]["disagreements"]
        ]
        self.assertEqual(suggested, [6, 7, 8])


class SerializationTests(unittest.TestCase):
    def test_the_verification_summary_survives_the_json_contract(self) -> None:
        """Every CLI subcommand prints JSON to stdout for the Rust layer, so
        anything map_job_episodes returns has to serialize cleanly."""
        summary = {
            "checked": 1,
            "confirmed": 0,
            "contradicted": 1,
            "disagreements": [
                {
                    "rip_title_id": 1,
                    "source_file": "B1_t00.mkv",
                    "assigned_episode_number": 1,
                    "assigned_title": "Perfect",
                    "suggested_season_number": 3,
                    "suggested_episode_number": 3,
                    "suggested_tmdb_episode_id": 503,
                    "suggested_title": "Dance Mode",
                    "score": 0.99,
                }
            ],
        }
        self.assertEqual(json.loads(json.dumps(summary)), summary)


if __name__ == "__main__":
    unittest.main()

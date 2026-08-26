"""
Tests for Plex naming and local finalization.

This module decides what every file ends up called and where it is placed, and
those names are what Plex matches against. A mistake here is quiet: the rip
succeeds, the transfer succeeds, and a wrongly-named episode lands on the NAS
where it will be mis-scraped or invisible. It had no tests at all.
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
from autorippr.naming import (  # noqa: E402
    _place_file,
    _sanitize_name,
    finalize_job_outputs,
    select_likely_movie_feature_rows,
)
from autorippr.state import create_job  # noqa: E402

NOW = "2026-08-26T00:00:00+00:00"


def build_config(root: Path, collision_policy: str = "skip") -> AppConfig:
    return AppConfig(
        tmdb_api_key="k",
        makemkv_path="x",
        ffmpeg_path="x",
        ffprobe_path="x",
        staging_root=str(root),
        nas_root=str(root / "nas"),
        db_path=str(root / "autorippr.db"),
        log_path=str(root / "autorippr.log"),
        collision_policy=collision_policy,
    )


class Row(dict):
    """Stands in for a sqlite3.Row, which is indexed by column name."""

    def __getitem__(self, key):
        return dict.__getitem__(self, key)


class SanitizeNameTests(unittest.TestCase):
    def test_strips_characters_windows_forbids(self) -> None:
        self.assertEqual(_sanitize_name('a<b>c:d"e/f\\g|h?i*j'), "abcdefghij")

    def test_collapses_whitespace(self) -> None:
        self.assertEqual(_sanitize_name("The   Long    Goodbye"), "The Long Goodbye")

    def test_trims_surrounding_space(self) -> None:
        self.assertEqual(_sanitize_name("  Episode One  "), "Episode One")

    def test_caps_length_so_the_path_stays_usable(self) -> None:
        """Windows paths cap near 260 chars; an unbounded title eats that."""
        self.assertEqual(len(_sanitize_name("x" * 500)), 180)

    def test_keeps_characters_that_are_legal(self) -> None:
        # Apostrophes, commas, dashes and brackets are all valid in filenames
        # and common in episode titles.
        self.assertEqual(_sanitize_name("Bart's Comet, Part 2 (Special)"), "Bart's Comet, Part 2 (Special)")


class PlaceFileTests(unittest.TestCase):
    def test_copies_and_creates_parents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src.mkv"
            src.write_bytes(b"data")
            target = root / "a" / "b" / "out.mkv"
            written, status, reason = _place_file(src, target, "skip")
            self.assertEqual(status, "ok")
            self.assertIsNone(reason)
            self.assertEqual(written.read_bytes(), b"data")

    def test_skip_policy_leaves_the_existing_file_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src.mkv"
            src.write_bytes(b"new")
            target = root / "out.mkv"
            target.write_bytes(b"original")
            written, status, reason = _place_file(src, target, "skip")
            self.assertEqual(status, "skipped")
            self.assertEqual(reason, "collision_skip")
            self.assertEqual(target.read_bytes(), b"original")

    def test_overwrite_policy_replaces_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src.mkv"
            src.write_bytes(b"new")
            target = root / "out.mkv"
            target.write_bytes(b"original")
            written, status, reason = _place_file(src, target, "overwrite")
            self.assertEqual(status, "ok")
            self.assertEqual(target.read_bytes(), b"new")


class FeatureSelectionTests(unittest.TestCase):
    """Picking the feature out of a disc full of extras."""

    def rows(self, *minutes):
        return [Row(duration_seconds=m * 60.0) for m in minutes]

    def test_prefers_anything_feature_length(self) -> None:
        picked = select_likely_movie_feature_rows(self.rows(112, 4, 9))
        self.assertEqual([r["duration_seconds"] for r in picked], [112 * 60.0])

    def test_keeps_several_features_on_a_double_bill(self) -> None:
        picked = select_likely_movie_feature_rows(self.rows(95, 88, 3))
        self.assertEqual(len(picked), 2)

    def test_short_feature_still_wins_when_it_dominates(self) -> None:
        """A 40 minute kids feature among 5 minute extras is still the feature."""
        picked = select_likely_movie_feature_rows(self.rows(40, 5, 4, 6))
        self.assertEqual([r["duration_seconds"] for r in picked], [40 * 60.0])

    def test_a_pile_of_similar_shorts_is_not_treated_as_a_feature(self) -> None:
        """Nothing dominates, so this must not silently pick one at random."""
        picked = select_likely_movie_feature_rows(self.rows(12, 11, 13, 12))
        self.assertEqual(picked, [])

    def test_missing_durations_fall_back_to_every_row(self) -> None:
        rows = [Row(duration_seconds=None), Row(duration_seconds=None)]
        self.assertEqual(len(select_likely_movie_feature_rows(rows)), 2)

    def test_no_rows_is_not_an_error(self) -> None:
        self.assertEqual(select_likely_movie_feature_rows([]), [])


class FinalizeTests(unittest.TestCase):
    """End-to-end naming, against a real database and real files."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = build_config(self.root)
        self.conn = open_db(self.cfg.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _source(self, name: str) -> Path:
        rip_dir = self.root / "rips"
        rip_dir.mkdir(parents=True, exist_ok=True)
        path = rip_dir / name
        path.write_bytes(b"video")
        return path

    def _select_media(self, job_id, media_type, title, year, season=None):
        self.conn.execute(
            "INSERT INTO job_selected_media (job_id, media_type, tmdb_id, title, year, season_number, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, media_type, 1, title, year, season, NOW, NOW),
        )

    def test_movie_gets_the_plex_folder_and_filename(self) -> None:
        job_id = create_job(self.conn, disc_label="D", media_type="movie")
        src = self._source("t00.mkv")
        self.conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
            (job_id, 0, 6720.0, str(src)),
        )
        self._select_media(job_id, "movie", "Barbie and the Magic of Pegasus", 2005)
        self.conn.commit()

        manifest = finalize_job_outputs(self.conn, self.cfg, job_id)

        written = Path(manifest["items"][0]["local_path"])
        self.assertEqual(written.name, "Barbie and the Magic of Pegasus (2005).mkv")
        self.assertEqual(written.parent.name, "Barbie and the Magic of Pegasus (2005)")
        self.assertTrue(written.exists())

    def test_movie_title_with_forbidden_characters_is_sanitized(self) -> None:
        job_id = create_job(self.conn, disc_label="D", media_type="movie")
        src = self._source("t00.mkv")
        self.conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
            (job_id, 0, 6720.0, str(src)),
        )
        self._select_media(job_id, "movie", 'Mission: Impossible <Special>', 1996)
        self.conn.commit()

        manifest = finalize_job_outputs(self.conn, self.cfg, job_id)
        name = Path(manifest["items"][0]["local_path"]).name
        for bad in '<>:"/\\|?*':
            self.assertNotIn(bad, name)
        self.assertIn("Mission Impossible", name)

    def _tv_job(self, episodes):
        """episodes: list of (start, end, [titles])."""
        job_id = create_job(self.conn, disc_label="D", media_type="tv", season_number=1)
        self._select_media(job_id, "tv", "Paw Patrol", 2013, season=1)
        for start, end, titles in episodes:
            src = self._source(f"t{start:02d}.mkv")
            self.conn.execute(
                "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
                (job_id, start, 1320.0, str(src)),
            )
            rip_id = self.conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            self.conn.execute(
                "INSERT INTO episode_mappings (job_id, rip_title_id, season_number, episode_start, episode_end, episode_titles_json) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, rip_id, 1, start, end, json.dumps(titles)),
            )
        self.conn.commit()
        return job_id

    def test_tv_episode_uses_plex_season_and_episode_tokens(self) -> None:
        job_id = self._tv_job([(1, 1, ["Pups Save a Train"])])
        manifest = finalize_job_outputs(self.conn, self.cfg, job_id)
        written = Path(manifest["items"][0]["local_path"])
        self.assertEqual(written.name, "Paw Patrol (2013) - s01e01 - Pups Save a Train.mkv")
        self.assertEqual(written.parent.name, "Season 01")
        self.assertEqual(written.parent.parent.name, "Paw Patrol (2013)")

    def test_combined_episode_gets_a_range_token(self) -> None:
        """Two episodes in one file must name both, or Plex sees only one."""
        job_id = self._tv_job([(3, 4, ["Pups Save the Day", "Pups Get a Lift"])])
        manifest = finalize_job_outputs(self.conn, self.cfg, job_id)
        name = Path(manifest["items"][0]["local_path"]).name
        self.assertIn("s01e03-e04", name)
        self.assertIn("Pups Save the Day & Pups Get a Lift", name)

    def test_missing_episode_title_falls_back_to_a_number(self) -> None:
        job_id = self._tv_job([(7, 7, [])])
        manifest = finalize_job_outputs(self.conn, self.cfg, job_id)
        name = Path(manifest["items"][0]["local_path"]).name
        self.assertIn("s01e07", name)
        self.assertIn("Episode 7", name)

    def test_season_number_is_zero_padded(self) -> None:
        job_id = create_job(self.conn, disc_label="D", media_type="tv", season_number=9)
        self._select_media(job_id, "tv", "Show", 2001, season=9)
        src = self._source("t01.mkv")
        self.conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
            (job_id, 1, 1320.0, str(src)),
        )
        rip_id = self.conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        self.conn.execute(
            "INSERT INTO episode_mappings (job_id, rip_title_id, season_number, episode_start, episode_end, episode_titles_json) "
            "VALUES (?,?,?,?,?,?)",
            (job_id, rip_id, 9, 2, 2, json.dumps(["Pilot"])),
        )
        self.conn.commit()
        manifest = finalize_job_outputs(self.conn, self.cfg, job_id)
        written = Path(manifest["items"][0]["local_path"])
        self.assertIn("s09e02", written.name)
        self.assertEqual(written.parent.name, "Season 09")

    def test_manifest_is_recorded_for_the_job(self) -> None:
        job_id = self._tv_job([(1, 1, ["One"])])
        finalize_job_outputs(self.conn, self.cfg, job_id)
        row = self.conn.execute(
            "SELECT manifest_json FROM finalized_manifests WHERE job_id = ?", (job_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["manifest_json"])["job_id"], job_id)

    def test_outputs_are_registered_for_transfer(self) -> None:
        """Nothing reaches the NAS unless finalize records an outputs row."""
        job_id = self._tv_job([(1, 1, ["One"]), (2, 2, ["Two"])])
        finalize_job_outputs(self.conn, self.cfg, job_id)
        rows = self.conn.execute(
            "SELECT local_path, transfer_status FROM outputs WHERE job_id = ?", (job_id,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["transfer_status"], "pending")
            self.assertTrue(Path(row["local_path"]).exists())


if __name__ == "__main__":
    unittest.main()

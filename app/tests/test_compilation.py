"""
Tests for compilation discs -- ones that draw episodes from across a show.

MINNIES_PET_SALON and I_HEART_MINNIE are Mickey Mouse Clubhouse DVDs, but
neither is a season and neither is a show: TMDB returns nothing at all for
either title. They are themed collections, a handful of episodes picked for
their subject from wherever in the run they happened to air.

The existing model could not express that. Mapping fetched one season, applied
a contiguous episode range to it, and stamped that single season number onto
every row it wrote. A disc holding "A Surprise for Minnie" (S01E02) alongside
"Minnie's Picnic" (S02E05) had no way to come out right.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr import mapper  # noqa: E402
from autorippr.mapper import EpisodeTarget, _season_for_row  # noqa: E402

# Mickey Mouse Clubhouse's real shape: 47 specials, then 26/39/32/26.
CLUBHOUSE_SEASONS = {
    "seasons": [
        {"season_number": 0, "episode_count": 47},
        {"season_number": 1, "episode_count": 26},
        {"season_number": 2, "episode_count": 39},
        {"season_number": 3, "episode_count": 32},
        {"season_number": 4, "episode_count": 26},
    ]
}


def fake_episodes(_conn, _cfg, _show_id, season_number):
    counts = {0: 47, 1: 26, 2: 39, 3: 32, 4: 26}
    return [
        {"episode_number": n, "id": season_number * 1000 + n, "name": f"S{season_number}E{n}"}
        for n in range(1, counts[season_number] + 1)
    ]


class CompilationEpisodePoolTests(unittest.TestCase):
    def _fetch(self, *, include_specials: bool):
        with patch.object(mapper, "_cached_show_detail", return_value=CLUBHOUSE_SEASONS), patch.object(
            mapper, "fetch_tmdb_tv_episodes", side_effect=fake_episodes
        ):
            return mapper._fetch_compilation_episodes(
                None, None, 3934, include_specials=include_specials
            )

    def test_the_whole_run_is_searchable(self) -> None:
        episodes = self._fetch(include_specials=False)
        self.assertEqual(len(episodes), 123)
        self.assertEqual(sorted({e["season_number"] for e in episodes}), [1, 2, 3, 4])

    def test_specials_are_left_out_unless_asked_for(self) -> None:
        # 47 specials against 123 episodes: pulling them in when the disc does
        # not draw on them is just more chances for a wrong name match.
        self.assertNotIn(0, {e["season_number"] for e in self._fetch(include_specials=False)})

    def test_specials_are_included_when_asked_for(self) -> None:
        episodes = self._fetch(include_specials=True)
        self.assertEqual(len(episodes), 170)
        self.assertIn(0, {e["season_number"] for e in episodes})

    def test_every_episode_carries_its_own_season(self) -> None:
        # Without this the file lands in the wrong season's folder.
        for episode in self._fetch(include_specials=True):
            self.assertIn("season_number", episode)


class PerRowSeasonTests(unittest.TestCase):
    """
    episode_mappings has always had a season per row -- it was just handed the
    same value every time. A compilation is what makes that matter.
    """

    TARGETS = [
        EpisodeTarget(episode_number=2, tmdb_episode_id=1002, title="A Surprise for Minnie", season_number=1),
        EpisodeTarget(episode_number=5, tmdb_episode_id=2005, title="Minnie's Picnic", season_number=2),
    ]

    def _lookup(self):
        return {t.tmdb_episode_id: t.season_number for t in self.TARGETS}

    def test_each_row_takes_the_season_of_what_it_matched(self) -> None:
        self.assertEqual(_season_for_row({"tmdb_episode_ids": [1002]}, self._lookup(), 1), 1)
        self.assertEqual(_season_for_row({"tmdb_episode_ids": [2005]}, self._lookup(), 1), 2)

    def test_two_episodes_from_one_disc_can_differ(self) -> None:
        # The whole point of the change: one disc, two seasons.
        seasons = {
            _season_for_row({"tmdb_episode_ids": [t.tmdb_episode_id]}, self._lookup(), 1)
            for t in self.TARGETS
        }
        self.assertEqual(seasons, {1, 2})

    def test_an_unmatched_row_keeps_the_job_season(self) -> None:
        self.assertEqual(_season_for_row({"tmdb_episode_ids": []}, self._lookup(), 3), 3)
        self.assertEqual(_season_for_row({}, self._lookup(), 3), 3)

    def test_an_unknown_episode_id_falls_back(self) -> None:
        self.assertEqual(_season_for_row({"tmdb_episode_ids": [999999]}, self._lookup(), 4), 4)

    def test_a_combined_title_takes_the_season_of_its_first_episode(self) -> None:
        row = {"tmdb_episode_ids": [1002, 2005]}
        self.assertEqual(_season_for_row(row, self._lookup(), 1), 1)

    def test_a_normal_disc_is_unaffected(self) -> None:
        # Every target shares a season, so this is a no-op there.
        same = [
            EpisodeTarget(episode_number=n, tmdb_episode_id=5000 + n, title=f"E{n}", season_number=2)
            for n in range(1, 6)
        ]
        lookup = {t.tmdb_episode_id: t.season_number for t in same}
        for target in same:
            self.assertEqual(_season_for_row({"tmdb_episode_ids": [target.tmdb_episode_id]}, lookup, 2), 2)


class EpisodeTargetTests(unittest.TestCase):
    def test_a_target_defaults_to_season_one(self) -> None:
        # Existing callers construct these without a season; they must not break.
        target = EpisodeTarget(episode_number=1, tmdb_episode_id=1, title="x")
        self.assertEqual(target.season_number, 1)


class PreselectShowTests(unittest.TestCase):
    """
    A show chosen in the disc card is an answer already given.

    Without carrying it to the job, a compilation disc would rip, fail to
    identify -- TMDB has no series called "Minnie's Pet Salon" -- and stop to
    ask the user the very question they answered before starting.
    """

    def _job(self, conn):
        from autorippr.state import create_job

        return create_job(conn, disc_label="MINNIES_PET_SALON", media_type="tv", disc_scope="compilation")

    def _preselect(self, conn, job_id):
        from autorippr import tmdb

        detail = {"tmdb_id": 3934, "name": "Mickey Mouse Clubhouse", "year": 2006, "seasons": []}
        with patch.object(tmdb, "fetch_tv_show_seasons", return_value=detail):
            return tmdb.preselect_tv_show(conn, None, job_id, 3934)

    def _conn(self):
        import tempfile
        from autorippr.db import open_db

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = open_db(str(Path(tmp.name) / "a.db"))
        self.addCleanup(conn.close)
        return conn

    def test_the_show_is_recorded_as_the_selection(self) -> None:
        conn = self._conn()
        job_id = self._job(conn)
        self._preselect(conn, job_id)
        selected = conn.execute(
            "SELECT media_type, tmdb_id, title FROM job_selected_media WHERE job_id = ?", (job_id,)
        ).fetchone()
        self.assertEqual(selected["tmdb_id"], 3934)
        self.assertEqual(selected["media_type"], "tv")
        self.assertEqual(selected["title"], "Mickey Mouse Clubhouse")

    def test_it_counts_as_a_manual_choice(self) -> None:
        # The pipeline recognises manual_override as "identification is
        # settled"; anything less and it would ask again after the rip.
        conn = self._conn()
        job_id = self._job(conn)
        self._preselect(conn, job_id)
        row = conn.execute(
            "SELECT selected, manual_override FROM tmdb_candidates WHERE job_id = ?", (job_id,)
        ).fetchone()
        self.assertEqual((row["selected"], row["manual_override"]), (1, 1))

    def test_a_label_that_matches_no_show_is_no_obstacle(self) -> None:
        # The entire point: MINNIES_PET_SALON returns nothing from TMDB, and
        # the job is identified anyway because the user said what it is.
        conn = self._conn()
        job_id = self._job(conn)
        self._preselect(conn, job_id)
        job = conn.execute("SELECT disc_label FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(job["disc_label"], "MINNIES_PET_SALON")
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM job_selected_media WHERE job_id = ?", (job_id,)).fetchone()
        )



class CrossSeasonNamingTests(unittest.TestCase):
    """
    A compilation's files must land in the season each episode belongs to.

    Naming read the season once, from the job, and used it for both the folder
    and the sNNeNN token on every file. That is correct for an ordinary disc
    and silently wrong for a compilation: "Minnie's Picnic" (S02E05) would be
    written as s01e05 into Season 01 -- wrong name, wrong folder, and nothing
    downstream to notice.
    """

    def _finalize(self, rows):
        import json as _json
        import tempfile
        from autorippr.db import open_db
        from autorippr.naming import _finalize_tv
        from autorippr.state import create_job

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        conn = open_db(str(root / "a.db"))
        self.addCleanup(conn.close)

        job_id = create_job(conn, disc_label="MMCH", media_type="tv", disc_scope="compilation")
        source_dir = root / "src"
        source_dir.mkdir()
        for index, (season, episode, name) in enumerate(rows):
            mkv = source_dir / f"t{index:02d}.mkv"
            mkv.write_text("data")
            rip_id = conn.execute(
                "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
                (job_id, index, 1500.0, str(mkv)),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO episode_mappings (
                    job_id, rip_title_id, season_number, episode_start, episode_end,
                    tmdb_episode_ids_json, episode_titles_json, confidence, reason,
                    manual_override, needs_split
                ) VALUES (?,?,?,?,?,?,?,?,?,0,0)
                """,
                (job_id, rip_id, season, episode, episode, "[]", _json.dumps([name]), 0.9, "test"),
            )
        conn.commit()

        out_root = root / "out"
        cfg = SimpleNamespace(collision_policy="skip")
        items = _finalize_tv(conn, cfg, job_id, out_root, "Mickey Mouse Clubhouse (2006)", 1)
        written = sorted(str(Path(i["local_path"]).relative_to(out_root)) for i in items)
        return written

    def test_episodes_are_filed_under_their_own_seasons(self) -> None:
        written = self._finalize([
            (1, 2, "A Surprise for Minnie"),
            (2, 5, "Minnie's Picnic"),
        ])
        joined = " | ".join(written)
        self.assertIn("Season 01", joined)
        self.assertIn("Season 02", joined)
        self.assertIn("s01e02", joined)
        self.assertIn("s02e05", joined)

    def test_no_episode_borrows_another_season_number(self) -> None:
        # The specific corruption: S02E05 written as s01e05.
        written = self._finalize([(1, 2, "A Surprise for Minnie"), (2, 5, "Minnie's Picnic")])
        self.assertNotIn("s01e05", " | ".join(written))

    def test_specials_get_their_own_folder(self) -> None:
        written = self._finalize([(0, 10, "Minnie's Bow-Tique"), (1, 2, "A Surprise for Minnie")])
        joined = " | ".join(written)
        self.assertIn("Season 00", joined)
        self.assertIn("s00e10", joined)

    def test_an_ordinary_single_season_disc_is_unchanged(self) -> None:
        written = self._finalize([(2, 1, "One"), (2, 2, "Two"), (2, 3, "Three")])
        self.assertTrue(all("Season 02" in path for path in written), written)
        self.assertEqual(len(written), 3)

if __name__ == "__main__":
    unittest.main()

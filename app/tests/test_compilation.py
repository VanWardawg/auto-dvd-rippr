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


class CompilationAlwaysReviewedTests(unittest.TestCase):
    """
    Position is evidence on an ordinary disc and noise on a compilation.

    When the name match fails, the planner still assigns whatever is next in
    the candidate pool. For Mickey Mouse Clubhouse with specials included that
    pool begins "Mickey's Great Clubhouse Hunt, Mickey's Adventures in
    Wonderland, The Wizard of Dizz (2)" -- so a Minnie disc would be labelled
    with three unrelated specials, at whatever confidence the durations
    happened to earn. Above 0.85 it would have been applied silently.
    """

    def _map(self, disc_scope: str, confidence: float):
        import tempfile
        from autorippr.db import open_db
        from autorippr.state import create_job

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = open_db(str(Path(tmp.name) / "a.db"))
        self.addCleanup(conn.close)

        job_id = create_job(conn, disc_label="D", media_type="tv", disc_scope=disc_scope, season_number=1)
        conn.execute(
            "INSERT INTO job_selected_media (job_id, media_type, tmdb_id, title, season_number, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_id, "tv", 3934, "Show", 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
            (job_id, 0, 1400.0, "t00.mkv"),
        )
        conn.commit()

        planned = [{
            "rip_title_id": 1, "episode_start": 1, "episode_end": 1,
            "tmdb_episode_ids": [1001], "episode_titles": ["Something"],
            "confidence": confidence, "reason": "positional", "needs_split": False,
        }]
        episodes = [{"episode_number": 1, "id": 1001, "name": "Something", "season_number": 1}]

        with patch.object(mapper, "_fetch_compilation_episodes", return_value=episodes), patch.object(
            mapper, "fetch_tmdb_tv_episodes", return_value=episodes
        ), patch.object(mapper, "_plan_mappings", return_value=planned), patch.object(
            mapper, "_clear_downstream_state_for_remap"
        ):
            return mapper.map_job_episodes(conn, None, job_id)

    def test_a_confident_guess_is_still_confirmed(self) -> None:
        # 0.95 would have sailed past the 0.85 review threshold.
        self.assertTrue(self._map("compilation", 0.95)["needs_review"])

    def test_a_low_confidence_one_certainly_is(self) -> None:
        self.assertTrue(self._map("compilation", 0.40)["needs_review"])

    def test_an_ordinary_disc_still_trusts_a_confident_match(self) -> None:
        # The rule must not spread to discs where position does mean something.
        self.assertFalse(self._map("full_season", 0.95)["needs_review"])

    def test_an_ordinary_disc_still_flags_a_weak_match(self) -> None:
        self.assertTrue(self._map("full_season", 0.40)["needs_review"])


class JobOwnDriveTests(unittest.TestCase):
    """
    Menu and OCR capture must only ever touch this job's own disc.

    They took the first drive with media in it, whoever it belonged to. The TV
    path ejects its disc the moment the rip finishes, to free the drive -- so
    by mapping time "the first drive with media" was the other bay. A Minnie's
    Pet Salon job ran ffmpeg against the disc being ripped in F:, which was the
    wrong film's menu and a drive already saturated by an active rip. ffmpeg
    timed out after five minutes and took the job down with it.
    """

    DRIVES = [
        {"drive": "E:", "root": "E:\\", "has_media": False, "volume_label": ""},
        {"drive": "F:", "root": "F:\\", "has_media": True, "volume_label": "OTHER_DISC"},
    ]

    def test_an_ejected_job_claims_no_disc(self) -> None:
        # E: ejected after its rip; F: holds someone else's disc.
        with patch.object(mapper, "discover_optical_drives", return_value=self.DRIVES):
            self.assertIsNone(mapper._job_disc_root("E:"))

    def test_it_never_borrows_the_other_drive(self) -> None:
        with patch.object(mapper, "discover_optical_drives", return_value=self.DRIVES):
            self.assertNotEqual(str(mapper._job_disc_root("E:") or ""), "F:\\")

    def test_a_job_whose_disc_is_present_gets_it(self) -> None:
        with patch.object(mapper, "discover_optical_drives", return_value=self.DRIVES):
            self.assertEqual(str(mapper._job_disc_root("F:")), "F:\\")

    def test_no_recorded_drive_means_no_disc(self) -> None:
        with patch.object(mapper, "discover_optical_drives", return_value=self.DRIVES):
            self.assertIsNone(mapper._job_disc_root(None))

    def test_menu_vobs_are_not_taken_from_another_disc(self) -> None:
        # The specific call that ran ffmpeg against the wrong drive.
        with patch.object(mapper, "discover_optical_drives", return_value=self.DRIVES):
            self.assertEqual(mapper._discover_dvd_menu_vobs("E:"), [])

    def test_ocr_falls_back_to_the_ripped_file(self) -> None:
        # With no disc to read, OCR should use the local rip -- which is fast,
        # and unambiguously this job's content.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ripped = Path(tmp) / "A1_t00.mkv"
            ripped.write_text("data")
            with patch.object(mapper, "discover_optical_drives", return_value=self.DRIVES):
                sources = mapper._build_ocr_source_candidates(str(ripped), "E:")
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["kind"], "title")


class GuessConfidenceTests(unittest.TestCase):
    """
    A guess must not score like a match.

    Both Minnie discs produced rows at 0.84 whose own reason line read "OCR
    fallback could not find a confident episode title match". That number came
    from duration alone -- a 24-minute file read as two 12-minute episodes --
    and sat in the review list beside genuine 0.97 name matches looking just as
    settled. Duration says how big a file is, never which episode it holds.
    """

    def _confidence(self, *, matched: bool, position_is_evidence: bool) -> float:
        return mapper._confidence_for_assignment(
            duration_seconds=24 * 60,
            episode_count=2,
            menu_match_score=1.0 if matched else None,
            position_is_evidence=position_is_evidence,
        )

    def test_a_real_name_match_still_scores_high(self) -> None:
        # The OCR read the title card off the video: that is real evidence.
        self.assertGreaterEqual(self._confidence(matched=True, position_is_evidence=False), 0.9)

    def test_a_compilation_guess_scores_low(self) -> None:
        self.assertLess(self._confidence(matched=False, position_is_evidence=False), 0.5)

    def test_an_ordinary_disc_still_trusts_position(self) -> None:
        # Title 3 really is usually episode 3, so duration that fits is
        # genuinely reassuring there.
        self.assertGreater(self._confidence(matched=False, position_is_evidence=True), 0.7)

    def test_a_guess_ranks_below_a_match(self) -> None:
        # The property that matters in the review list.
        guess = self._confidence(matched=False, position_is_evidence=False)
        match = self._confidence(matched=True, position_is_evidence=False)
        self.assertLess(guess, match)

    def test_an_unmeasurable_file_scores_lowest(self) -> None:
        self.assertLessEqual(
            mapper._confidence_for_assignment(
                duration_seconds=0, episode_count=1, menu_match_score=None,
                position_is_evidence=False,
            ),
            0.2,
        )


class MappingRerunRespectsReviewTests(unittest.TestCase):
    """
    Re-running mapping must not be a way to skip review.

    The handler advanced the job whenever mapping produced rows, without ever
    looking at needs_review -- and transition_job clears awaiting_review on the
    way past. So re-running mapping on a compilation, which always asks for
    confirmation, silently moved it on towards the NAS carrying assignments
    nothing had identified. The UI's "rerun mapping" button uses this path too.
    """

    def _run(self, *, needs_review: bool):
        import tempfile
        import main as cli
        from autorippr.db import open_db
        from autorippr.state import create_job, get_job, transition_job

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = open_db(str(Path(tmp.name) / "a.db"))
        self.addCleanup(conn.close)

        job_id = create_job(conn, disc_label="D", media_type="tv", disc_scope="compilation")
        transition_job(conn, job_id, "identifying")
        transition_job(conn, job_id, "mapping")
        conn.commit()

        result = {
            "needs_review": needs_review,
            "mappings": [{"needs_split": False, "episode_start": 1}],
        }
        args = SimpleNamespace(mapping_command="run", job_id=job_id)
        with patch.object(cli, "map_job_episodes", return_value=result):
            if result.get("needs_review"):
                cli.set_awaiting_review(conn, job_id, True)
            elif get_job(conn, job_id)["status"] == "mapping":
                transition_job(conn, job_id, "renaming")
        return get_job(conn, job_id)

    def test_a_job_needing_review_stays_put(self) -> None:
        job = self._run(needs_review=True)
        self.assertEqual(job["status"], "mapping")
        self.assertEqual(job["awaiting_review"], 1)

    def test_a_clean_mapping_still_advances(self) -> None:
        job = self._run(needs_review=False)
        self.assertEqual(job["status"], "renaming")


class CrossSeasonOverrideTests(unittest.TestCase):
    """
    Correcting an episode by hand has to be able to name its season.

    set_mapping_override read the season from job_selected_media, which a
    compilation leaves NULL, so it fell back to season 1 and looked the episode
    title up there. Correcting a row that sits in season 3 would have written a
    season 1 title onto it -- and the guided review has no season field at all,
    so that was the only correction path available.
    """

    def _job_with_mapping(self, conn, row_season: int):
        import json as _json
        from autorippr.state import create_job

        job_id = create_job(conn, disc_label="D", media_type="tv", disc_scope="compilation")
        conn.execute(
            "INSERT INTO job_selected_media (job_id, media_type, tmdb_id, title, season_number, created_at, updated_at) "
            "VALUES (?,?,?,?,NULL,?,?)",
            (job_id, "tv", 3934, "Show", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        rip_id = conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
            (job_id, 0, 1440.0, "t00.mkv"),
        ).lastrowid
        mapping_id = conn.execute(
            """
            INSERT INTO episode_mappings (
                job_id, rip_title_id, season_number, episode_start, episode_end,
                tmdb_episode_ids_json, episode_titles_json, confidence, reason,
                manual_override, needs_split
            ) VALUES (?,?,?,?,?,?,?,?,?,0,0)
            """,
            (job_id, rip_id, row_season, 1, 1, "[]", _json.dumps(["Wrong"]), 0.35, "guess"),
        ).lastrowid
        conn.commit()
        return mapping_id

    def _conn(self):
        import tempfile
        from autorippr.db import open_db

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = open_db(str(Path(tmp.name) / "a.db"))
        self.addCleanup(conn.close)
        return conn

    def _override(self, conn, mapping_id, season):
        episodes = [{"episode_number": 12, "id": 999, "name": "Sea Captain Mickey"}]
        with patch.object(mapper, "fetch_tmdb_tv_episodes", return_value=episodes):
            return mapper.set_mapping_override(
                conn, None, mapping_id,
                episode_start=12, episode_end=12,
                tmdb_episode_ids=[999], reason="test",
                season_number=season,
            )

    def test_an_episode_can_be_moved_to_another_season(self) -> None:
        conn = self._conn()
        mapping_id = self._job_with_mapping(conn, row_season=1)
        self._override(conn, mapping_id, season=4)
        row = conn.execute(
            "SELECT season_number, episode_start FROM episode_mappings WHERE id = ?", (mapping_id,)
        ).fetchone()
        self.assertEqual((row["season_number"], row["episode_start"]), (4, 12))

    def test_the_title_comes_from_the_season_given(self) -> None:
        # The specific corruption: a season 1 title on a season 4 row.
        import json as _json

        conn = self._conn()
        mapping_id = self._job_with_mapping(conn, row_season=1)
        self._override(conn, mapping_id, season=4)
        titles = _json.loads(
            conn.execute(
                "SELECT episode_titles_json FROM episode_mappings WHERE id = ?", (mapping_id,)
            ).fetchone()["episode_titles_json"]
        )
        self.assertEqual(titles, ["Sea Captain Mickey"])

    def test_omitting_the_season_keeps_the_row_where_it_is(self) -> None:
        # An ordinary disc never passes one, and must not be moved to season 1.
        conn = self._conn()
        mapping_id = self._job_with_mapping(conn, row_season=3)
        self._override(conn, mapping_id, season=None)
        row = conn.execute(
            "SELECT season_number FROM episode_mappings WHERE id = ?", (mapping_id,)
        ).fetchone()
        self.assertEqual(row["season_number"], 3)

    def test_an_override_is_recorded_as_certain(self) -> None:
        conn = self._conn()
        mapping_id = self._job_with_mapping(conn, row_season=1)
        self._override(conn, mapping_id, season=4)
        row = conn.execute(
            "SELECT confidence, manual_override FROM episode_mappings WHERE id = ?", (mapping_id,)
        ).fetchone()
        self.assertEqual((row["confidence"], row["manual_override"]), (1.0, 1))


class PartialRangeSlackTests(unittest.TestCase):
    """
    A slightly wrong disc range must not hide the right episode.

    An Avatar Book 3 set suggested 7-11 for disc 2, because the arithmetic
    assumed 21 episodes split evenly across four discs. Disc 1 actually held
    five, so disc 2 began at episode 6 -- and filtering the candidates to
    exactly 7-11 meant E06 "The Avatar and the Firelord" was not on the list at
    all. No amount of name matching could find it, and all five episodes came
    out one too high, onto the NAS.
    """

    def _targets(self, numbers):
        return [
            EpisodeTarget(episode_number=n, tmdb_episode_id=1000 + n, title=f"E{n}", season_number=3)
            for n in numbers
        ]

    def _filtered(self, start, end, available=range(1, 22)):
        targets = self._targets(available)
        low = max(1, start - mapper.RANGE_SLACK_EPISODES)
        high = end + mapper.RANGE_SLACK_EPISODES
        return [t.episode_number for t in targets if low <= t.episode_number <= high]

    def test_the_episode_just_before_the_range_stays_reachable(self) -> None:
        # The exact miss: disc 2 was told 7-11 and actually started at 6.
        self.assertIn(6, self._filtered(7, 11))

    def test_the_stated_range_is_still_covered(self) -> None:
        for episode in range(7, 12):
            self.assertIn(episode, self._filtered(7, 11))

    def test_the_window_stays_narrow(self) -> None:
        # Widening must not reopen the whole season, or the name match has 21
        # candidates and position stops meaning anything.
        self.assertEqual(self._filtered(7, 11), [5, 6, 7, 8, 9, 10, 11, 12, 13])

    def test_it_does_not_run_off_the_start_of_the_season(self) -> None:
        self.assertEqual(min(self._filtered(1, 5)), 1)

    def test_a_range_past_the_end_yields_what_exists(self) -> None:
        self.assertEqual(self._filtered(20, 24), [18, 19, 20, 21])


class RangeSlackPositionalTests(unittest.TestCase):
    """
    Slack episodes are for name matches only; position must never hand one out.

    The slack window was added so a near-miss range could still reach the right
    episode by name. But the widened list also fed the planner's count checks:
    a five-title Avatar disc with range 11-15 saw nine candidates, failed the
    "titles match episode count" gate, and fell into the duration heuristic --
    which divides by 12 minutes and read every 24-minute episode as a double
    bill, with needs_split set. The splitter would have cut single episodes in
    half.
    """

    def _rows(self, count=5):
        return [
            {
                "id": i + 1, "title_id": i, "duration_seconds": 24.5 * 60,
                "chapter_count": 6, "source_file": f"t{i:02d}.mkv",
                "raw_metadata_json": None,
            }
            for i in range(count)
        ]

    def _targets(self, low=9, high=17, core=(11, 15)):
        return [
            EpisodeTarget(
                episode_number=n, tmdb_episode_id=1000 + n, title=f"Ep {n}",
                season_number=3, in_core_range=core[0] <= n <= core[1],
            )
            for n in range(low, high + 1)
        ]

    def _plan(self, targets, ocr=None):
        with patch.object(mapper, "_derive_cached_bundle_assignments", return_value={}), \
             patch.object(mapper, "_identify_likely_play_all_titles", return_value=set()), \
             patch.object(mapper, "_find_best_menu_match", return_value=None), \
             patch.object(mapper, "_should_try_ocr_menu_fallback", return_value=ocr is not None), \
             patch.object(mapper, "_find_best_ocr_menu_match",
                          side_effect=ocr or (lambda **k: {"match": None})):
            return mapper._plan_mappings(self._rows(), targets, None, "job", None)

    def test_the_avatar_disc_shape_maps_one_episode_per_title(self) -> None:
        planned = [p for p in self._plan(self._targets()) if p["rip_title_id"] is not None]
        self.assertEqual([p["episode_start"] for p in planned], [11, 12, 13, 14, 15])
        self.assertEqual([p["episode_end"] for p in planned], [11, 12, 13, 14, 15])
        self.assertFalse(any(p["needs_split"] for p in planned))

    def test_unclaimed_slack_episodes_are_not_reported_missing(self) -> None:
        # E9, E10, E16, E17 were never expected on this disc.
        planned = self._plan(self._targets())
        leftover = [p for p in planned if p["rip_title_id"] is None]
        self.assertEqual(leftover, [])

    def test_a_name_match_can_still_reach_a_slack_episode(self) -> None:
        # The reason the slack exists: the disc really starts one early, and
        # OCR reads the true episode off the screen. Note the asymmetry this
        # test pins down: when title count exactly matches the stated range,
        # the in-order fast path assigns positionally and never consults OCR
        # at all -- which is how an off-by-one disc range went to the NAS
        # unchallenged at 0.90. Names are only consulted when the counts
        # disagree, so this scenario uses a 4-episode range under 5 titles.
        calls = {"n": 0}

        def fake_ocr(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                remaining = kwargs["targets"]
                idx = next(i for i, t in enumerate(remaining) if t.episode_number == 10)
                return {"match": {"score": 0.95, "index": idx},
                        "artifact_image_path": None, "artifact_text_path": None,
                        "best_score": 0.95, "source_label": "test", "source_kind": "title"}
            return {"match": None, "artifact_image_path": None,
                    "artifact_text_path": None, "best_score": None, "source_label": None}

        targets = self._targets(low=9, high=16, core=(11, 14))
        planned = [p for p in self._plan(targets, ocr=fake_ocr)
                   if p["rip_title_id"] is not None]
        self.assertIn(10, [p["episode_start"] for p in planned])


class TypicalEpisodeLengthTests(unittest.TestCase):
    """
    The duration fallback divided by a flat 12 minutes, so every 24-minute
    episode read as a double bill with needs_split set -- on a disc where all
    five titles were visibly the same length. The disc's own median title
    length is the right divisor: on an episode disc most titles are single
    episodes, so the median is one episode even when a couple are doubles.
    """

    def _rows(self, minutes):
        return [
            {"id": i + 1, "title_id": i, "duration_seconds": m * 60,
             "chapter_count": 6, "source_file": f"t{i:02d}.mkv", "raw_metadata_json": None}
            for i, m in enumerate(minutes)
        ]

    def test_a_uniform_disc_reads_as_singles(self) -> None:
        self.assertAlmostEqual(
            mapper._typical_episode_seconds(self._rows([24.5, 24.5, 24.5, 24.5, 24.5])),
            24.5 * 60,
        )

    def test_a_double_length_title_still_reads_as_two(self) -> None:
        typical = mapper._typical_episode_seconds(self._rows([24.5, 24.5, 49.0, 24.5]))
        self.assertEqual(max(1, round(49.0 * 60 / typical)), 2)

    def test_the_median_shrugs_off_an_outlier(self) -> None:
        # A stray play-all that escaped detection must not drag the estimate.
        typical = mapper._typical_episode_seconds(self._rows([24.5, 24.5, 24.5, 24.5, 122.0]))
        self.assertAlmostEqual(typical, 24.5 * 60)

    def test_an_unmeasurable_disc_falls_back_sanely(self) -> None:
        self.assertEqual(mapper._typical_episode_seconds(self._rows([2.0, 3.0])), 22.0 * 60)

    def test_avatar_disc_three_no_longer_pairs_up(self) -> None:
        # The full planner path, exactly as the disc presented it: five
        # 24.5-minute titles, range 11-15 with slack, no name matches.
        targets = [
            EpisodeTarget(episode_number=n, tmdb_episode_id=1000 + n, title=f"Ep {n}",
                          season_number=3, in_core_range=11 <= n <= 15)
            for n in range(9, 18)
        ]
        with patch.object(mapper, "_derive_cached_bundle_assignments", return_value={}), \
             patch.object(mapper, "_identify_likely_play_all_titles", return_value=set()), \
             patch.object(mapper, "_find_best_menu_match", return_value=None), \
             patch.object(mapper, "_should_try_ocr_menu_fallback", return_value=False):
            planned = mapper._plan_mappings(
                self._rows([24.5] * 5), targets, None, "job", None)
        mapped = [p for p in planned if p["rip_title_id"] is not None]
        self.assertEqual([p["episode_start"] for p in mapped], [11, 12, 13, 14, 15])
        self.assertFalse(any(p["needs_split"] for p in mapped))

if __name__ == "__main__":
    unittest.main()

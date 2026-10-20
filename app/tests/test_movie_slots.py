"""
Tests for assigning ripped files to the movie slots of a double feature.

A real double-feature disc was named backwards: the slots were filled by
MakeMKV title number while the UI listed the files in a different order and
never said which file a slot would use, so there was no way to catch it before
both movies reached the NAS under each other's names. The user can now pick a
slot's file; these tests pin that the pick is what gets named, and that two
slots can never end up on the same file.
"""

import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.db import open_db  # noqa: E402
from autorippr.naming import _assign_movie_slots, plan_movie_slot_sources  # noqa: E402
from autorippr.state import create_job  # noqa: E402
from autorippr.tmdb import TmdbError, list_selected_movie_slots, select_tmdb_candidate  # noqa: E402


def rip_row(row_id: int, title_id: int, minutes: float) -> dict:
    return {"id": row_id, "title_id": title_id, "duration_seconds": minutes * 60}


class PlanMovieSlotSourcesTests(unittest.TestCase):
    # Ordered by title number, as the callers fetch them. Row ids are in rip
    # order, which is not title order -- exactly the disc that went wrong.
    ROWS = [rip_row(2, 0, 74.4), rip_row(1, 2, 75.2)]

    def test_defaults_follow_disc_order(self) -> None:
        plan = plan_movie_slot_sources(self.ROWS, [1, 2], {})
        self.assertEqual(plan, {1: 2, 2: 1})

    def test_explicit_choice_wins_and_other_slot_takes_the_remaining_file(self) -> None:
        # The bug this guards: a positional zip gave slot 2 the second row
        # too, so both slots were named from the same file.
        plan = plan_movie_slot_sources(self.ROWS, [1, 2], {1: 1, 2: None})
        self.assertEqual(plan, {1: 1, 2: 2})

    def test_explicit_choice_on_later_slot_is_not_taken_by_an_earlier_default(self) -> None:
        plan = plan_movie_slot_sources(self.ROWS, [1, 2], {1: None, 2: 2})
        self.assertEqual(plan, {1: 1, 2: 2})

    def test_extras_are_not_offered_as_defaults(self) -> None:
        rows = [rip_row(1, 0, 74.0), rip_row(2, 1, 2.2), rip_row(3, 2, 75.0)]
        self.assertEqual(plan_movie_slot_sources(rows, [1, 2], {}), {1: 1, 2: 3})

    def test_unknown_choice_is_ignored(self) -> None:
        self.assertEqual(plan_movie_slot_sources(self.ROWS, [1, 2], {1: 99}), {1: 2, 2: 1})

    def test_unfillable_slot_is_left_out(self) -> None:
        self.assertEqual(plan_movie_slot_sources(self.ROWS[:1], [1, 2], {}), {1: 2})


class MovieSlotSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = open_db(str(Path(self._tmp.name) / "autorippr.db"))
        self.job_id = create_job(self.conn, disc_label="combo", media_type="movie", movie_mode="double_feature")
        # Insert in rip order: title 2 is saved first and gets row id 1.
        self.conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
            (self.job_id, 2, 75.2 * 60, "C1_t02.mkv"),
        )
        self.conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
            (self.job_id, 0, 74.4 * 60, "D1_t00.mkv"),
        )
        for tmdb_id, title in ((21956, "Monster of Mexico"), (24615, "Aloha")):
            self.conn.execute(
                "INSERT INTO tmdb_candidates (job_id, tmdb_id, media_type, title, year, score) VALUES (?,?,?,?,?,?)",
                (self.job_id, tmdb_id, "movie", title, 2003, 0.5),
            )
        self.conn.commit()
        self.rip_id = {
            str(r["source_file"]): int(r["id"])
            for r in self.conn.execute("SELECT id, source_file FROM rip_titles").fetchall()
        }

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def slots(self):
        return self.conn.execute(
            "SELECT slot_index, tmdb_id, title, year, rip_title_id FROM job_selected_movies "
            "WHERE job_id = ? ORDER BY slot_index",
            (self.job_id,),
        ).fetchall()

    def test_chosen_files_are_the_ones_named(self) -> None:
        mexico, aloha = self.rip_id["C1_t02.mkv"], self.rip_id["D1_t00.mkv"]
        select_tmdb_candidate(self.conn, self.job_id, 21956, "movie", 1, mexico)
        select_tmdb_candidate(self.conn, self.job_id, 24615, "movie", 2, aloha)
        assigned = {a["slot_index"]: a["rip_title_id"] for a in _assign_movie_slots(self.conn, self.job_id, self.slots())}
        self.assertEqual(assigned, {1: mexico, 2: aloha})

    def test_choosing_a_file_held_by_another_slot_swaps_them(self) -> None:
        mexico, aloha = self.rip_id["C1_t02.mkv"], self.rip_id["D1_t00.mkv"]
        select_tmdb_candidate(self.conn, self.job_id, 21956, "movie", 1, aloha)
        select_tmdb_candidate(self.conn, self.job_id, 24615, "movie", 2, mexico)
        select_tmdb_candidate(self.conn, self.job_id, 21956, "movie", 1, mexico)
        held = {s["slot_index"]: s["rip_title_id"] for s in list_selected_movie_slots(self.conn, self.job_id)}
        self.assertEqual(held, {1: mexico, 2: aloha})

    def test_reselecting_without_a_file_keeps_the_slots_file(self) -> None:
        mexico = self.rip_id["C1_t02.mkv"]
        select_tmdb_candidate(self.conn, self.job_id, 21956, "movie", 1, mexico)
        select_tmdb_candidate(self.conn, self.job_id, 24615, "movie", 1)
        self.assertEqual(list_selected_movie_slots(self.conn, self.job_id)[0]["rip_title_id"], mexico)

    def test_file_from_another_job_is_rejected(self) -> None:
        other = create_job(self.conn, disc_label="other", media_type="movie")
        self.conn.execute(
            "INSERT INTO rip_titles (job_id, title_id, source_file) VALUES (?,?,?)", (other, 0, "other.mkv")
        )
        foreign = int(self.conn.execute("SELECT id FROM rip_titles WHERE job_id = ?", (other,)).fetchone()["id"])
        with self.assertRaises(TmdbError):
            select_tmdb_candidate(self.conn, self.job_id, 21956, "movie", 1, foreign)


if __name__ == "__main__":
    unittest.main()

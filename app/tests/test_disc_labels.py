"""
Tests for turning a disc's volume label into the right film.

Both cases here come from one evening's Alvin and the Chipmunks discs.

ALVIN_AND_THE_CHIPMUNKS_4X3 carries an aspect-ratio marker. Searching TMDB
for "alvin and the chipmunks 4x3" returned nothing at all, so the job stopped
and waited for a human, who typed the same title without the 4x3 and got it
immediately.

ALVIN_AND_THE_CHIPMUNKS_3 is the third film -- which TMDB calls "Alvin and the
Chipmunks: Chipwrecked", with no digit in it anywhere. The base 2007 film
therefore scored highest, and at 0.786 against a 0.75 threshold it was accepted
silently. Nothing caught it except the NAS refusing to overwrite the 2007 film
that a previous disc had already put there.
"""

import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.tmdb import (  # noqa: E402
    _franchise_position,
    _normalize_query,
    _sequel_number_unexplained,
)


def query(label: str) -> str:
    return _normalize_query(label, preserve_numbers=True)


class DiscJunkTokenTests(unittest.TestCase):
    def test_the_aspect_ratio_marker_is_dropped(self) -> None:
        self.assertEqual(query("ALVIN_AND_THE_CHIPMUNKS_4X3"), "alvin and the chipmunks")

    def test_the_other_aspect_ratio_is_dropped_too(self) -> None:
        self.assertEqual(query("EVER_AFTER_16X9"), "ever after")

    def test_edition_suffixes_are_dropped(self) -> None:
        # From the collection: PRINCESS_BRIDE_CE.
        self.assertEqual(query("PRINCESS_BRIDE_CE"), "princess bride")
        self.assertEqual(query("BRAVEHEART_SE"), "braveheart")

    def test_picture_format_and_standard_are_dropped(self) -> None:
        self.assertEqual(query("THE_MATRIX_WS"), "the matrix")
        self.assertEqual(query("SOME_FILM_NTSC"), "some film")

    def test_a_sequel_number_is_not_junk(self) -> None:
        # The whole difficulty: 4X3 must go, 3 must stay.
        self.assertEqual(query("TOY_STORY_3"), "toy story 3")
        self.assertEqual(query("SHREK_2"), "shrek 2")

    def test_stripping_never_empties_a_title(self) -> None:
        # A title made entirely of tokens that look like junk must survive as
        # something searchable rather than becoming an empty query.
        for label in ("WS", "CE", "PAL"):
            self.assertIsInstance(query(label), str)


class SequelNumberTests(unittest.TestCase):
    ALVIN = [
        {"title": "Alvin and the Chipmunks", "year": 2007, "tmdb_id": 6477},
        {"title": "Alvin and the Chipmunks: The Squeakquel", "year": 2009, "tmdb_id": 23398},
        {"title": "Alvin and the Chipmunks: Chipwrecked", "year": 2011, "tmdb_id": 50321},
    ]

    def _ask(self, label, candidate, ranked=None):
        return _sequel_number_unexplained(label, candidate, ranked if ranked is not None else [])

    def test_the_bug_the_base_film_for_a_numbered_disc(self) -> None:
        # 0.786 was enough to accept this silently. It is the wrong film.
        self.assertTrue(self._ask("ALVIN_AND_THE_CHIPMUNKS_3", self.ALVIN[0], self.ALVIN))

    def test_the_right_sequel_is_accepted_without_a_question(self) -> None:
        # Chipwrecked prints no number, but it is the third film released
        # under that name, which accounts for the label just as well.
        self.assertFalse(self._ask("ALVIN_AND_THE_CHIPMUNKS_3", self.ALVIN[2], self.ALVIN))

    def test_the_wrong_sequel_is_still_caught(self) -> None:
        self.assertTrue(self._ask("ALVIN_AND_THE_CHIPMUNKS_3", self.ALVIN[1], self.ALVIN))

    def test_a_title_carrying_the_digit_explains_itself(self) -> None:
        self.assertFalse(self._ask("TOY_STORY_3", {"title": "Toy Story 3", "year": 2010, "tmdb_id": 3}))

    def test_roman_numerals_count_as_the_number(self) -> None:
        self.assertFalse(self._ask("ROCKY_2", {"title": "Rocky II", "year": 1979, "tmdb_id": 1}))

    def test_a_spelled_out_number_counts_too(self) -> None:
        self.assertFalse(
            self._ask("HOME_ALONE_2", {"title": "Home Alone Two", "year": 1992, "tmdb_id": 1})
        )

    def test_a_disc_with_no_number_is_never_questioned(self) -> None:
        self.assertFalse(self._ask("THE_MATRIX", {"title": "The Matrix", "year": 1999, "tmdb_id": 9}))

    def test_the_aspect_ratio_marker_is_not_read_as_a_sequel(self) -> None:
        # _4X3 must not look like "the third film" once 4x3 is stripped.
        self.assertFalse(
            self._ask("ALVIN_AND_THE_CHIPMUNKS_4X3", self.ALVIN[0], self.ALVIN)
        )

    def test_a_year_at_the_end_is_not_a_sequel_number(self) -> None:
        self.assertFalse(
            self._ask("SOME_FILM_1997", {"title": "Some Film", "year": 1997, "tmdb_id": 1})
        )


class FranchisePositionTests(unittest.TestCase):
    ALVIN = SequelNumberTests.ALVIN

    def test_release_order_places_each_film(self) -> None:
        self.assertEqual(_franchise_position(self.ALVIN[0], self.ALVIN), 1)
        self.assertEqual(_franchise_position(self.ALVIN[2], self.ALVIN), 3)

    def test_a_missing_year_refuses_to_order(self) -> None:
        # Ordering is the entire answer here, so guessing at it would be worse
        # than declining -- a wrong auto-pick puts a mis-named file on the NAS.
        polluted = self.ALVIN + [{"title": "Alvin and the Chipmunks: Extra", "year": None, "tmdb_id": 99}]
        self.assertIsNone(_franchise_position(self.ALVIN[2], polluted))

    def test_a_lone_film_has_no_franchise_position(self) -> None:
        single = [{"title": "The Matrix", "year": 1999, "tmdb_id": 9}]
        self.assertIsNone(_franchise_position(single[0], single))

    def test_unrelated_films_are_not_counted_as_siblings(self) -> None:
        mixed = self.ALVIN + [{"title": "Something Else Entirely", "year": 1990, "tmdb_id": 77}]
        self.assertEqual(_franchise_position(self.ALVIN[2], mixed), 3)


if __name__ == "__main__":
    unittest.main()

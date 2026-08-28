"""
Tests for turning a disc's volume label into the right title.

A DVD's volume label is written by whoever authored the pressing, so it
carries their concerns rather than the viewer's: aspect ratio, edition,
season and disc numbers. Every case here is a real disc whose label sent the
app somewhere wrong.

The film cases come from one evening's Alvin and the Chipmunks discs.

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
    _normalize_identify_query,
    parse_disc_hints,
    suggest_episode_range,
    _normalize_query,
    _sequel_number_unexplained,
)


def query(label: str) -> str:
    return _normalize_query(label, preserve_numbers=True)


def tv_query(label: str) -> str:
    return _normalize_identify_query(label, "tv")


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



class TvLabelTests(unittest.TestCase):
    """
    Season and disc markers are the standard DVD convention, and both of the
    ways the app read them were broken.

    THE_WINGFEATHER_SAGA_S1 searched TMDB for "the wingfeather saga s1" and
    got nothing back, so the job stopped for a human. The season was lost at
    the same time, for a different reason: an underscore is a word character,
    so `\bs(\d+)\b` finds no boundary in SAGA_S1 and matched nothing.
    """

    def test_the_season_marker_is_kept_out_of_the_query(self) -> None:
        self.assertEqual(tv_query("THE_WINGFEATHER_SAGA_S1"), "the wingfeather saga")

    def test_the_disc_marker_is_kept_out_too(self) -> None:
        self.assertEqual(tv_query("TUTTLE_TWINS_S1_D2"), "tuttle twins")

    def test_spelled_out_markers_were_already_handled(self) -> None:
        self.assertEqual(tv_query("PAW_PATROL_SEASON_3_DISC_2"), "paw patrol")

    def test_the_season_survives_the_underscore(self) -> None:
        self.assertEqual(parse_disc_hints("THE_WINGFEATHER_SAGA_S1").detected_season, 1)
        self.assertEqual(parse_disc_hints("MICKEY_MOUSE_CLUBHOUSE_S2_D3").detected_season, 2)

    def test_the_disc_number_is_read(self) -> None:
        # Which disc of the set decides the episode range, so it is worth
        # knowing before asking the user to work one out.
        self.assertEqual(parse_disc_hints("TUTTLE_TWINS_S1_D2").detected_disc, 2)
        self.assertEqual(parse_disc_hints("PAW_PATROL_SEASON_3_DISC_2").detected_disc, 2)

    def test_two_discs_of_one_season_differ_only_by_disc(self) -> None:
        first = parse_disc_hints("TUTTLE_TWINS_S1_D1")
        second = parse_disc_hints("TUTTLE_TWINS_S1_D2")
        self.assertEqual(first.detected_season, second.detected_season)
        self.assertEqual(first.normalized_query, second.normalized_query)
        self.assertNotEqual(first.detected_disc, second.detected_disc)

    def test_an_episode_code_gives_the_season(self) -> None:
        self.assertEqual(parse_disc_hints("SOME_SHOW_S02E05").detected_season, 2)

    def test_a_compilation_disc_claims_no_season(self) -> None:
        # Minnie's Pet Salon is a themed collection, not a season. Inventing a
        # season number for it would be worse than admitting there is none.
        for label in ("MINNIES_PET_SALON", "I_HEART_MINNIE"):
            hints = parse_disc_hints(label)
            self.assertIsNone(hints.detected_season, label)
            self.assertIsNone(hints.detected_disc, label)

    def test_a_compilation_title_is_left_intact(self) -> None:
        self.assertEqual(tv_query("MINNIES_PET_SALON"), "minnies pet salon")
        self.assertEqual(tv_query("I_HEART_MINNIE"), "i heart minnie")


class EpisodeRangeSuggestionTests(unittest.TestCase):
    """
    Working out which episodes are on disc 3 of 4 is a step the user currently
    does by hand, on TMDB, before they can start the job.
    """

    def test_a_season_splits_evenly_across_its_discs(self) -> None:
        self.assertEqual(suggest_episode_range(24, 1, 4), (1, 6))
        self.assertEqual(suggest_episode_range(24, 4, 4), (19, 24))

    def test_the_remainder_goes_to_the_earlier_discs(self) -> None:
        # Mickey Mouse Clubhouse season 2: 39 episodes over 4 discs is
        # 10/10/10/9, which is how sets are actually cut.
        ranges = [suggest_episode_range(39, n, 4) for n in (1, 2, 3, 4)]
        self.assertEqual(ranges, [(1, 10), (11, 20), (21, 30), (31, 39)])

    def test_the_ranges_tile_the_season_exactly(self) -> None:
        # No episode may be dropped between discs or claimed by two of them.
        for count, discs in ((26, 3), (39, 4), (32, 5), (13, 2)):
            covered: list[int] = []
            for n in range(1, discs + 1):
                start, end = suggest_episode_range(count, n, discs)
                covered.extend(range(start, end + 1))
            self.assertEqual(covered, list(range(1, count + 1)), f"{count} eps over {discs} discs")

    def test_a_single_disc_season_is_the_whole_season(self) -> None:
        self.assertEqual(suggest_episode_range(26, 1, 1), (1, 26))

    def test_it_declines_rather_than_guessing(self) -> None:
        # Without the disc number there is nothing to base a range on, and a
        # wrong prefill is worse than an empty box the user fills in.
        self.assertIsNone(suggest_episode_range(26, None, 4))
        self.assertIsNone(suggest_episode_range(26, 2, None))
        self.assertIsNone(suggest_episode_range(0, 1, 4))

    def test_a_disc_outside_the_set_is_refused(self) -> None:
        self.assertIsNone(suggest_episode_range(26, 5, 4))
        self.assertIsNone(suggest_episode_range(26, 0, 4))

    def test_more_discs_than_episodes_is_refused(self) -> None:
        self.assertIsNone(suggest_episode_range(3, 1, 8))

if __name__ == "__main__":
    unittest.main()


class BookAndVolumeLabelTests(unittest.TestCase):
    """
    Avatar's discs are labelled AVATAR_BK3_VOL1: book 3, volume 1.

    Neither token was recognised, so a four-disc season set produced no season
    and no disc number, and the episode range could not be suggested at all.
    """

    def test_a_book_is_a_season(self) -> None:
        # Avatar and much anime number seasons as books.
        self.assertEqual(parse_disc_hints("AVATAR_BK3_VOL1").detected_season, 3)
        self.assertEqual(parse_disc_hints("AVATAR_BOOK_3_VOLUME_4").detected_season, 3)

    def test_a_volume_is_a_disc(self) -> None:
        self.assertEqual(parse_disc_hints("AVATAR_BK3_VOL1").detected_disc, 1)
        self.assertEqual(parse_disc_hints("AVATAR_BK3_VOL2").detected_disc, 2)
        self.assertEqual(parse_disc_hints("AVATAR_BOOK_3_VOLUME_4").detected_disc, 4)

    def test_neither_reaches_the_search_query(self) -> None:
        self.assertEqual(tv_query("AVATAR_BK3_VOL1"), "avatar")

    def test_the_four_discs_differ_only_by_volume(self) -> None:
        hints = [parse_disc_hints(f"AVATAR_BK3_VOL{n}") for n in (1, 2, 3, 4)]
        self.assertEqual({h.detected_season for h in hints}, {3})
        self.assertEqual([h.detected_disc for h in hints], [1, 2, 3, 4])

    def test_the_existing_conventions_still_work(self) -> None:
        self.assertEqual(parse_disc_hints("MICKEY_MOUSE_CLUBHOUSE_S2_D3").detected_season, 2)
        self.assertEqual(parse_disc_hints("MICKEY_MOUSE_CLUBHOUSE_S2_D3").detected_disc, 3)

    def test_a_book_in_a_title_is_not_a_season(self) -> None:
        # "The Jungle Book" has no digit after it, so nothing is claimed.
        self.assertIsNone(parse_disc_hints("THE_JUNGLE_BOOK").detected_season)

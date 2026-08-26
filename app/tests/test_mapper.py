"""
Tests for the episode-mapping decision logic.

mapper.py is the largest module in the project and decides which ripped title
becomes which episode. Most of it drives OCR, VLC and external tools and needs
a disc in the drive, but the judgement calls underneath are pure functions --
and those are where a wrong answer silently mislabels an episode.

These cover the judgement, not the plumbing: how a menu label is matched to an
episode title, when a label is too generic to trust, how confident an
assignment is, and which titles are "play all" tracks rather than episodes.
"""

import json
import sys
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.mapper import (  # noqa: E402
    EpisodeTarget,
    _best_contiguous_multi_match,
    _clean_title_hint_line,
    _confidence_for_assignment,
    _episode_token_overlap_score,
    _extract_menu_name,
    _find_best_menu_match,
    _identify_likely_play_all_titles,
    _normalize_name,
    _parse_raw_metadata,
    _score_title_hint_text,
    _should_try_ocr_menu_fallback,
)


def targets(*titles):
    return [EpisodeTarget(episode_number=i + 1, title=t, tmdb_episode_id=i + 1)
            for i, t in enumerate(titles)]


class Row(dict):
    """Stands in for a sqlite3.Row, which is indexed by column name."""

    def __getitem__(self, key):
        return dict.__getitem__(self, key)


class NormalizeNameTests(unittest.TestCase):
    def test_lowercases_and_flattens_separators(self) -> None:
        self.assertEqual(_normalize_name("PAW_PATROL-S01.E02"), "paw patrol s01 e02")

    def test_drops_punctuation(self) -> None:
        self.assertEqual(_normalize_name("Pups Save a Train!"), "pups save a train")

    def test_collapses_runs_of_space(self) -> None:
        self.assertEqual(_normalize_name("A    B"), "a b")

    def test_all_punctuation_normalizes_to_empty(self) -> None:
        self.assertEqual(_normalize_name("!!!___!!!"), "")


class MenuMatchTests(unittest.TestCase):
    """Matching a DVD menu label against the season's episode titles."""

    def test_exact_title_matches_its_episode(self) -> None:
        best = _find_best_menu_match("Pups Save a Train", targets("Pups Save the Sea Turtles", "Pups Save a Train"))
        self.assertIsNotNone(best)
        self.assertEqual(best["index"], 1)

    def test_close_but_imperfect_label_still_matches(self) -> None:
        """Menus abbreviate and drop punctuation; that must not break matching."""
        best = _find_best_menu_match("PUPS SAVE THE SEA TURTLES", targets("Pups Save the Sea Turtles", "Other"))
        self.assertIsNotNone(best)
        self.assertEqual(best["index"], 0)

    def test_unrelated_label_matches_nothing(self) -> None:
        """Below threshold must return None rather than the least-bad guess."""
        self.assertIsNone(_find_best_menu_match("Special Features", targets("Pups Save a Train", "Pups and the Kitty")))

    def test_empty_label_matches_nothing(self) -> None:
        self.assertIsNone(_find_best_menu_match("", targets("Anything")))

    def test_no_targets_matches_nothing(self) -> None:
        self.assertIsNone(_find_best_menu_match("Pups Save a Train", []))


class OcrFallbackTests(unittest.TestCase):
    """Deciding whether a menu label carries any real information."""

    def test_real_titles_do_not_need_ocr(self) -> None:
        for name in ("Pups Save a Train", "The Great Escape", "Bart Sells His Soul"):
            with self.subTest(name=name):
                self.assertFalse(_should_try_ocr_menu_fallback(name))

    def test_makemkv_style_identifiers_need_ocr(self) -> None:
        """A1_t00 and friends carry no title information at all."""
        for name in ("A1_t00", "B7", "Title 3", "t12", "A2 t05", ""):
            with self.subTest(name=name):
                self.assertTrue(_should_try_ocr_menu_fallback(name))

    def test_missing_label_needs_ocr(self) -> None:
        self.assertTrue(_should_try_ocr_menu_fallback(None))

    def test_generic_words_alone_need_ocr(self) -> None:
        self.assertTrue(_should_try_ocr_menu_fallback("Chapter"))


class PlayAllDetectionTests(unittest.TestCase):
    """A play-all track duplicates content already captured per episode."""

    def rows(self, *specs):
        return [Row(id=i + 1, duration_seconds=d * 60.0, source_file=f) for i, (d, f) in enumerate(specs)]

    def test_long_makemkv_named_title_is_play_all(self) -> None:
        rows = self.rows((22, "A1_t00.mkv"), (22, "A1_t01.mkv"), (22, "A1_t02.mkv"), (66, "A1_t03.mkv"))
        self.assertEqual(_identify_likely_play_all_titles(rows), {4})

    def test_a_feature_length_movie_alone_is_not_play_all(self) -> None:
        rows = self.rows((112, "A1_t00.mkv"))
        self.assertEqual(_identify_likely_play_all_titles(rows), set())

    def test_episodes_of_similar_length_yield_nothing(self) -> None:
        rows = self.rows((22, "A1_t00.mkv"), (22, "A1_t01.mkv"), (23, "A1_t02.mkv"))
        self.assertEqual(_identify_likely_play_all_titles(rows), set())

    def test_long_title_with_a_different_naming_scheme_is_left_alone(self) -> None:
        """The heuristic is deliberately tied to MakeMKV's naming."""
        rows = self.rows((22, "ep1.mkv"), (22, "ep2.mkv"), (66, "playall.mkv"))
        self.assertEqual(_identify_likely_play_all_titles(rows), set())


class ConfidenceTests(unittest.TestCase):
    """Confidence drives whether a job stops to ask the user."""

    def test_a_strong_menu_match_beats_duration_reasoning(self) -> None:
        self.assertGreaterEqual(_confidence_for_assignment(1320.0, 1, 0.95), 0.95)

    def test_confidence_rises_with_menu_match_quality(self) -> None:
        weak = _confidence_for_assignment(1320.0, 1, 0.71)
        mid = _confidence_for_assignment(1320.0, 1, 0.85)
        strong = _confidence_for_assignment(1320.0, 1, 0.95)
        self.assertLess(weak, mid)
        self.assertLess(mid, strong)

    def test_the_high_confidence_band_is_short_form_content(self) -> None:
        """
        The 6-20 minute band is what earns real confidence. That fits the
        library this was built against (kids shows, Barbie, Paw Patrol) but
        means a 22 minute sitcom lands exactly on the default 0.75 threshold
        and a 44 minute drama falls to 0.52, i.e. straight to manual review.
        Recorded here so the tuning is visible rather than surprising.
        """
        self.assertEqual(_confidence_for_assignment(11 * 60.0, 1, None), 0.92)
        self.assertEqual(_confidence_for_assignment(22 * 60.0, 1, None), 0.75)
        self.assertEqual(_confidence_for_assignment(44 * 60.0, 1, None), 0.52)

    def test_an_odd_length_is_not_trusted(self) -> None:
        """Three minutes per episode is not an episode; it should ask."""
        self.assertLess(_confidence_for_assignment(180.0, 1, None), 0.75)

    def test_unknown_duration_is_low_confidence(self) -> None:
        self.assertLess(_confidence_for_assignment(0.0, 1, None), 0.5)

    def test_a_combined_short_form_title_is_slightly_less_certain(self) -> None:
        """Only inside the short-form band; outside it both collapse to 0.75."""
        single = _confidence_for_assignment(11 * 60.0, 1, None)
        combined = _confidence_for_assignment(22 * 60.0, 2, None)
        self.assertLess(combined, single)


class RawMetadataTests(unittest.TestCase):
    def test_parses_a_json_string(self) -> None:
        self.assertEqual(_parse_raw_metadata('{"a": 1}'), {"a": 1})

    def test_passes_a_dict_through(self) -> None:
        self.assertEqual(_parse_raw_metadata({"a": 1}), {"a": 1})

    def test_malformed_json_yields_an_empty_dict(self) -> None:
        self.assertEqual(_parse_raw_metadata("{not json"), {})

    def test_none_yields_an_empty_dict(self) -> None:
        self.assertEqual(_parse_raw_metadata(None), {})

    def test_menu_name_preferred_over_makemkv_display_name(self) -> None:
        raw = {"menu_name": "Pups Save a Train", "makemkv_info": {"display_name": "A1_t00.mkv"}}
        self.assertEqual(_extract_menu_name(raw), "Pups Save a Train")

    def test_falls_back_to_the_makemkv_display_name(self) -> None:
        self.assertEqual(_extract_menu_name({"makemkv_info": {"display_name": "A1_t00.mkv"}}), "A1_t00.mkv")

    def test_blank_values_are_treated_as_absent(self) -> None:
        self.assertIsNone(_extract_menu_name({"menu_name": "   ", "makemkv_info": {}}))


class TitleHintTests(unittest.TestCase):
    """Filtering OCR output down to lines that could be a title."""

    def test_keeps_a_plausible_title(self) -> None:
        self.assertEqual(_clean_title_hint_line("  Barbie of Swan Lake  "), "Barbie of Swan Lake")

    def test_strips_surrounding_punctuation(self) -> None:
        self.assertEqual(_clean_title_hint_line("*** Swan Lake ***"), "Swan Lake")

    def test_rejects_the_menu_furniture_it_knows_about(self) -> None:
        for line in ("Chapter 3", "Setup", "DVD Menu", "Title 1"):
            with self.subTest(line=line):
                self.assertIsNone(_clean_title_hint_line(line))

    def test_common_menu_furniture_still_gets_through(self) -> None:
        """
        Known gap. The stop list catches single technical words but not these
        two-word menu labels, so "Play All" can be offered to TMDB as a movie
        title hint. Low impact -- _score_title_hint_text ranks them poorly, so
        they only win when nothing better was found -- but that is exactly the
        disc where a bad hint does damage. Recorded rather than fixed while
        rips are in flight; the fix is to extend the stop list.
        """
        for line in ("Play All", "Play Movie", "Special Features", "Scene Selection"):
            with self.subTest(line=line):
                self.assertIsNotNone(_clean_title_hint_line(line))

    def test_rejects_lines_too_short_to_be_a_title(self) -> None:
        self.assertIsNone(_clean_title_hint_line("ok"))

    def test_rejects_ocr_noise(self) -> None:
        self.assertIsNone(_clean_title_hint_line("!!!***"))

    def test_longer_wordier_text_scores_higher(self) -> None:
        """The scorer picks which OCR line is most likely to be the title."""
        title = _score_title_hint_text("Barbie of Swan Lake", _normalize_name("Barbie of Swan Lake"))
        noise = _score_title_hint_text("a b c", _normalize_name("a b c"))
        self.assertGreater(title, noise)

    def test_text_with_no_real_words_scores_zero(self) -> None:
        self.assertEqual(_score_title_hint_text("12 34", _normalize_name("12 34")), 0)


class TokenOverlapTests(unittest.TestCase):
    def test_identical_text_scores_one(self) -> None:
        self.assertEqual(_episode_token_overlap_score("pups save a train", "pups save a train"), 1.0)

    def test_partial_overlap_scores_between(self) -> None:
        score = _episode_token_overlap_score("pups save the train today", "pups save a train")
        self.assertGreater(score, 0.5)
        self.assertLessEqual(score, 1.0)

    def test_unrelated_text_scores_zero(self) -> None:
        self.assertEqual(_episode_token_overlap_score("completely different words", "pups save train"), 0.0)

    def test_short_tokens_are_ignored(self) -> None:
        """Two-letter words carry no signal and would inflate every score."""
        self.assertEqual(_episode_token_overlap_score("a b c", "of to it"), 0.0)


class ContiguousMatchTests(unittest.TestCase):
    """A menu listing consecutive episodes is strong evidence of ordering."""

    def match(self, index, score):
        return {"index": index, "score": score, "episode": f"ep{index}"}

    def test_finds_the_longest_consecutive_run(self) -> None:
        best = _best_contiguous_multi_match([
            self.match(0, 0.9), self.match(1, 0.9), self.match(2, 0.9), self.match(7, 0.95),
        ])
        self.assertIsNotNone(best)
        self.assertEqual(best["indices"], [0, 1, 2])

    def test_a_single_match_is_not_a_run(self) -> None:
        self.assertIsNone(_best_contiguous_multi_match([self.match(3, 0.99)]))

    def test_non_consecutive_matches_are_not_a_run(self) -> None:
        self.assertIsNone(_best_contiguous_multi_match([self.match(0, 0.9), self.match(4, 0.9)]))

    def test_a_weakly_scored_run_is_rejected(self) -> None:
        """Consecutive but uncertain is not evidence worth acting on."""
        self.assertIsNone(_best_contiguous_multi_match([self.match(0, 0.5), self.match(1, 0.5)]))

    def test_empty_input_is_not_an_error(self) -> None:
        self.assertIsNone(_best_contiguous_multi_match([]))


if __name__ == "__main__":
    unittest.main()

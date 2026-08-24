"""
Tests for runtime-based disambiguation of TMDB candidates.

The ripped runtime is the strongest evidence available for a movie disc, and
it was previously discarded: scoring applied a coarse prior based on the ripped
duration alone and never compared it to the candidate.

The safety half matters more than the accuracy half. Using runtime to break
ties makes it easy to become *confidently wrong* -- a disc labelled "SINBAD"
matches several films actually titled "Sinbad", none of which is the one the
user wants, and picking the closest runtime among them writes a mis-named file
to the NAS. Asking costs one click; being wrong costs a corrupted library.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.tmdb import (  # noqa: E402
    TmdbError,
    _apply_runtime_to_candidate,
    _fetch_candidate_runtime,
    _same_title_lacks_runtime_proof,
    _score_runtime_match,
)

WEIGHTS = {
    "title_similarity": 0.52,
    "year_proximity": 0.18,
    "runtime_fit": 0.12,
    "season_hint": 0.10,
    "popularity": 0.05,
    "votes": 0.03,
}


def candidate(title, year, *, score=0.75, delta=None, lookup=None, media_type="movie", tmdb_id=1):
    breakdown = {"weights": dict(WEIGHTS), "runtime_fit": 0.5}
    if delta is not None:
        breakdown["runtime_delta_minutes"] = delta
    if lookup is not None:
        breakdown["runtime_lookup"] = lookup
    return {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "year": year,
        "score": score,
        "score_breakdown": breakdown,
    }


class ScoreRuntimeMatchTests(unittest.TestCase):
    def test_exact_match_scores_full(self) -> None:
        self.assertEqual(_score_runtime_match(101.0, 101.0), 1.0)

    def test_pal_speedup_still_counts_as_a_match(self) -> None:
        """A 100 min film runs ~96 min on a PAL disc; that must not be a miss."""
        self.assertGreaterEqual(_score_runtime_match(96.0, 100.0), 0.6)

    def test_wildly_different_runtime_scores_zero(self) -> None:
        self.assertEqual(_score_runtime_match(42.0, 140.0), 0.0)

    def test_unknown_runtime_is_neutral(self) -> None:
        self.assertEqual(_score_runtime_match(0.0, 100.0), 0.5)
        self.assertEqual(_score_runtime_match(100.0, 0.0), 0.5)

    def test_closer_runtime_never_scores_lower(self) -> None:
        previous = None
        for delta in range(0, 40, 2):
            value = _score_runtime_match(100.0 + delta, 100.0)
            if previous is not None:
                self.assertLessEqual(value, previous)
            previous = value


class ApplyRuntimeTests(unittest.TestCase):
    def test_exact_match_raises_the_score(self) -> None:
        c = candidate("Wreck-It Ralph", 2012, score=0.73)
        _apply_runtime_to_candidate(c, 101.0, 101.0)
        self.assertGreater(c["score"], 0.73)
        self.assertEqual(c["score_breakdown"]["runtime_delta_minutes"], 0.0)
        self.assertEqual(c["score_breakdown"]["runtime_fit"], 1.0)

    def test_bad_match_lowers_the_score(self) -> None:
        c = candidate("Some Film", 1999, score=0.73)
        _apply_runtime_to_candidate(c, 42.0, 140.0)
        self.assertLess(c["score"], 0.73)

    def test_score_shift_is_bounded_by_the_weight(self) -> None:
        """Runtime must inform the ranking, not dominate it."""
        c = candidate("Some Film", 1999, score=0.50)
        _apply_runtime_to_candidate(c, 100.0, 100.0)
        self.assertLessEqual(c["score"] - 0.50, WEIGHTS["runtime_fit"] + 1e-9)

    def test_malformed_candidate_is_ignored(self) -> None:
        c = {"title": "x", "score": 0.5}
        _apply_runtime_to_candidate(c, 100.0, 100.0)
        self.assertEqual(c["score"], 0.5)


class FetchRuntimeTests(unittest.TestCase):
    def test_movie_runtime(self) -> None:
        with patch("autorippr.tmdb._cached_tmdb_get", return_value={"runtime": 118}):
            self.assertEqual(_fetch_candidate_runtime(None, None, candidate("A", 2000)), 118.0)

    def test_tv_uses_episode_run_time(self) -> None:
        with patch("autorippr.tmdb._cached_tmdb_get", return_value={"episode_run_time": [44, 45]}):
            c = candidate("Show", 2001, media_type="tv")
            self.assertEqual(_fetch_candidate_runtime(None, None, c), 44.0)

    def test_missing_runtime_returns_none(self) -> None:
        for payload in ({"runtime": None}, {"runtime": 0}, {}, {"episode_run_time": []}):
            with patch("autorippr.tmdb._cached_tmdb_get", return_value=payload):
                self.assertIsNone(_fetch_candidate_runtime(None, None, candidate("A", 2000)))

    def test_api_failure_does_not_propagate(self) -> None:
        """A detail lookup failing must never sink identification."""
        with patch("autorippr.tmdb._cached_tmdb_get", side_effect=TmdbError("boom")):
            self.assertIsNone(_fetch_candidate_runtime(None, None, candidate("A", 2000)))


class SameTitleGuardTests(unittest.TestCase):
    def test_unique_title_is_not_ambiguous(self) -> None:
        ranked = [candidate("Batman Begins", 2005, delta=0.0, lookup="resolved"),
                  candidate("Batman", 1989, delta=20.0, lookup="resolved", tmdb_id=2)]
        self.assertFalse(_same_title_lacks_runtime_proof(ranked, "movie"))

    def test_sinbad_case_is_blocked(self) -> None:
        """The right film is absent entirely; a 5 min match must not convince us."""
        ranked = [
            candidate("Sinbad", 1971, score=0.76, delta=5.0, lookup="resolved"),
            candidate("Sinbad", 2012, score=0.66, delta=44.0, lookup="resolved", tmdb_id=2),
        ]
        self.assertTrue(_same_title_lacks_runtime_proof(ranked, "movie"))

    def test_decisive_exact_match_is_allowed(self) -> None:
        ranked = [
            candidate("Robin Hood", 1973, score=0.82, delta=0.0, lookup="resolved"),
            candidate("Robin Hood", 2010, score=0.70, delta=57.0, lookup="resolved", tmdb_id=2),
        ]
        self.assertFalse(_same_title_lacks_runtime_proof(ranked, "movie"))

    def test_near_rival_blocks_even_an_exact_match(self) -> None:
        """Robin Hood 1973 (83m) vs 1991 (86m): PAL speedup could explain it."""
        ranked = [
            candidate("Robin Hood", 1973, score=0.82, delta=0.0, lookup="resolved"),
            candidate("Robin Hood", 1991, score=0.78, delta=3.0, lookup="resolved", tmdb_id=2),
        ]
        self.assertTrue(_same_title_lacks_runtime_proof(ranked, "movie"))

    def test_two_identical_runtimes_are_unresolvable(self) -> None:
        """Overboard 1987 and 2018 both run 112 minutes."""
        ranked = [
            candidate("Overboard", 2018, score=0.80, delta=0.0, lookup="resolved"),
            candidate("Overboard", 1987, score=0.79, delta=0.0, lookup="resolved", tmdb_id=2),
        ]
        self.assertTrue(_same_title_lacks_runtime_proof(ranked, "movie"))

    def test_rival_without_tmdb_runtime_does_not_veto(self) -> None:
        """An unreleased entry TMDB has no runtime for is not a real rival."""
        ranked = [
            candidate("Mamma Mia!", 2008, score=0.80, delta=1.0, lookup="resolved"),
            candidate("Mamma Mia!", 2024, score=0.70, lookup="unavailable", tmdb_id=2),
        ]
        self.assertFalse(_same_title_lacks_runtime_proof(ranked, "movie"))

    def test_unchecked_rival_does_veto(self) -> None:
        """Never having looked is different from having looked and found nothing."""
        ranked = [
            candidate("Robin Hood", 1973, score=0.82, delta=0.0, lookup="resolved"),
            candidate("Robin Hood", 1991, score=0.72, tmdb_id=2),  # no lookup stamp
        ]
        self.assertTrue(_same_title_lacks_runtime_proof(ranked, "movie"))

    def test_no_runtime_at_all_blocks_same_title(self) -> None:
        ranked = [
            candidate("Robin Hood", 1973, score=0.82, lookup="unavailable"),
            candidate("Robin Hood", 1991, score=0.78, lookup="unavailable", tmdb_id=2),
        ]
        self.assertTrue(_same_title_lacks_runtime_proof(ranked, "movie"))

    def test_tv_is_out_of_scope(self) -> None:
        ranked = [candidate("Show", 2001, media_type="tv"),
                  candidate("Show", 2010, media_type="tv", tmdb_id=2)]
        self.assertFalse(_same_title_lacks_runtime_proof(ranked, "tv"))


if __name__ == "__main__":
    unittest.main()

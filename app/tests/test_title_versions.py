"""
Discs that carry more than one cut of a movie.

Deathly Hallows Part 2 has the 130-minute film (00100.mpls) and a 167-minute
"Maximum Movie Mode" playlist (00200.mpls) that splices featurettes into it.
Longest-wins ripped Maximum Movie Mode. Which cut belongs in the library is a
person's call -- the extended cut is the right answer on other discs -- so a
single-movie job with several cuts now stops before ripping and asks.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr import pipeline, rip  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.makemkv import build_title_candidates, feature_versions, parse_audio_languages  # noqa: E402
from autorippr.state import create_job, get_job, transition_job  # noqa: E402
from test_single_episode import build_config  # noqa: E402


NAME = "Harry Potter and the Deathly Hallows Part 2"


def disc_info_text(titles: list[tuple[int, str, str, str]]) -> str:
    """(title id, length, source playlist, size) -> MakeMKV `info` output."""
    lines = []
    for title_id, length, source, size in titles:
        lines += [
            f'TINFO:{title_id},2,0,"{NAME}"',
            f'TINFO:{title_id},9,0,"{length}"',
            f'TINFO:{title_id},10,0,"{size}"',
            f'TINFO:{title_id},16,0,"{source}"',
            f'TINFO:{title_id},27,0,"{NAME}_t{title_id:02d}.mkv"',
        ]
    return "\n".join(lines) + "\n"


# The real disc, trimmed to the titles that matter.
DISC = [
    (0, "2:47:25", "00200.mpls", "23.3 GB"),
    (1, "0:26:27", "00220.mpls", "2.0 GB"),
    (2, "2:10:26", "00100.mpls", "22.4 GB"),
    (3, "0:03:27", "00218.mpls", "272.1 MB"),
]


class FeatureVersionTests(unittest.TestCase):
    def versions(self, titles):
        return [v.title_id for v in feature_versions(build_title_candidates(rip._parse_makemkv_info_output(disc_info_text(titles))))]

    def test_maximum_movie_mode_is_a_second_version(self) -> None:
        self.assertEqual(self.versions(DISC), [0, 2])

    def test_copies_of_one_cut_are_one_version(self) -> None:
        titles = [(0, "2:10:26", "00100.mpls", "22.4 GB"), (1, "2:10:31", "00101.mpls", "22.4 GB"), (2, "0:03:00", "x", "1 MB")]
        self.assertEqual(len(self.versions(titles)), 1)

    def test_extras_are_not_versions(self) -> None:
        self.assertEqual(self.versions([(0, "2:10:26", "a", "22 GB"), (1, "0:26:27", "b", "2 GB")]), [0])


class AudioLanguageTests(unittest.TestCase):
    def test_reads_audio_tracks_only(self) -> None:
        text = "\n".join(
            [
                'SINFO:2,0,1,6201,"Video"',
                'SINFO:2,1,1,6202,"Audio"',
                'SINFO:2,1,4,0,"English"',
                'SINFO:2,2,1,6202,"Audio"',
                'SINFO:2,2,4,0,"French"',
                'SINFO:2,3,1,6203,"Subtitles"',
                'SINFO:2,3,4,0,"Spanish"',
            ]
        )
        self.assertEqual(parse_audio_languages(text), {2: ["English", "French"]})


class VersionChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = build_config(self.root)
        self.conn = open_db(self.cfg.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def job(self, movie_mode: str = "single") -> str:
        job_id = create_job(self.conn, disc_label="HARRY_POTTER_7_PART_2", media_type="movie", movie_mode=movie_mode)
        transition_job(self.conn, job_id, "ripping")
        self.conn.commit()
        return job_id

    def scan(self, job_id: str, titles) -> dict:
        text = disc_info_text(titles)
        logs = self.root / "jobs" / job_id / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "makemkv_disc_info.log").write_text(text, encoding="utf-8")
        return rip._parse_makemkv_info_output(text)

    def plan(self, job_id: str, info: dict):
        with patch.object(rip, "lookup_movie_runtime_minutes", return_value=130.0):
            return rip._plan_title_selection(self.conn, self.cfg, job_id, info)

    def test_several_cuts_pause_before_ripping(self) -> None:
        job_id = self.job()
        with self.assertRaises(rip.TitleChoiceNeeded):
            self.plan(job_id, self.scan(job_id, DISC))
        self.assertTrue(get_job(self.conn, job_id)["awaiting_review"])

    def test_the_cut_matching_tmdb_is_suggested(self) -> None:
        job_id = self.job()
        with self.assertRaises(rip.TitleChoiceNeeded):
            self.plan(job_id, self.scan(job_id, DISC))
        described = rip.describe_title_versions(self.conn, self.cfg, get_job(self.conn, job_id))
        self.assertTrue(described["pending"])
        self.assertEqual(described["suggested_title_id"], 2)
        self.assertEqual([v["title_id"] for v in described["versions"]], [0, 2])

    def test_the_chosen_cut_is_ripped(self) -> None:
        job_id = self.job()
        info = self.scan(job_id, DISC)
        with self.assertRaises(rip.TitleChoiceNeeded):
            self.plan(job_id, info)
        rip.choose_title_version(self.conn, self.cfg, job_id, 2)
        self.assertFalse(get_job(self.conn, job_id)["awaiting_review"])
        self.assertEqual(self.plan(job_id, info)[0], [2])

    def test_the_choice_follows_the_playlist_when_titles_renumber(self) -> None:
        job_id = self.job()
        with self.assertRaises(rip.TitleChoiceNeeded):
            self.plan(job_id, self.scan(job_id, DISC))
        rip.choose_title_version(self.conn, self.cfg, job_id, 2)
        renumbered = [(0, "2:47:25", "00200.mpls", "23.3 GB"), (1, "2:10:26", "00100.mpls", "22.4 GB")]
        self.assertEqual(self.plan(job_id, self.scan(job_id, renumbered))[0], [1])

    def test_a_title_that_is_not_a_version_is_refused(self) -> None:
        job_id = self.job()
        self.scan(job_id, DISC)
        with self.assertRaises(rip.RipError):
            rip.choose_title_version(self.conn, self.cfg, job_id, 3)

    def test_double_features_are_unaffected(self) -> None:
        job_id = self.job(movie_mode="double_feature")
        selected, _ = self.plan(job_id, self.scan(job_id, DISC))
        self.assertEqual(sorted(selected), [0, 2])

    def test_one_cut_rips_without_asking(self) -> None:
        job_id = self.job()
        selected, _ = self.plan(job_id, self.scan(job_id, [(0, "2:10:26", "00100.mpls", "22 GB"), (1, "0:03:00", "x", "1 MB")]))
        self.assertEqual(selected, [0])

    def test_leftover_rip_of_an_unchosen_cut_is_not_recovered(self) -> None:
        job_id = self.job()
        info = self.scan(job_id, DISC)
        ripped = [rip.RipTitleMetadata(title_id=0, duration_seconds=10045.0, chapter_count=41, source_file="x", raw_metadata={})]
        self.assertFalse(rip._recovered_rip_is_the_chosen_version(self.conn, job_id, ripped, info))
        rip.choose_title_version(self.conn, self.cfg, job_id, 0)
        self.assertTrue(rip._recovered_rip_is_the_chosen_version(self.conn, job_id, ripped, info))

    def test_the_pipeline_waits_instead_of_erroring(self) -> None:
        job_id = self.job()
        with patch.object(pipeline, "recover_completed_rip", return_value=None), patch.object(
            pipeline, "_warn_if_nas_unreachable"
        ), patch.object(pipeline, "execute_rip_job", side_effect=rip.TitleChoiceNeeded("pick")):
            result = pipeline.run_pipeline_for_job(self.conn, self.cfg, job_id)
        self.assertEqual(result["status"], "ripping")
        self.assertEqual(get_job(self.conn, job_id)["status"], "ripping")


if __name__ == "__main__":
    unittest.main()

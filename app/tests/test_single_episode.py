"""
Tests for single-episode discs.

Some collections put one story on each disc -- Living Scriptures Animated
Stories has ~80 discs of one ~30-minute episode each. Mapped as a full season,
every such disc came out as "episode 1" (the only position evidence there is)
at a confidence that paused it for review, so every disc needed a manual pick.
A single-episode job carries the episode the user chose at the start and must
map straight to it without asking.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr import mapper  # noqa: E402
from autorippr.config import AppConfig  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.mapper import MappingError  # noqa: E402
from autorippr.state import create_job  # noqa: E402


EPISODES = [
    {"id": 1000 + n, "episode_number": n, "name": name, "runtime": 30}
    for n, name in enumerate(
        [
            "Nephi and the Brass Plates", "The Tree of Life", "Journey to the Promised Land",
            "Abinadi and King Noah", "The Conversion of Alma the Younger",
            "Ammon, Missionary to the Lamanites", "Alma and the Zoramites",
            "Helaman's Stripling Warriors", "Samuel the Lamanite",
        ],
        start=1,
    )
]


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


class SingleEpisodeMappingTests(unittest.TestCase):
    def run_mapping(self, *, minutes, disc_scope="single_episode", episode=8):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root)
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(
                    conn,
                    disc_label="HELAMANS_WARRIORS",
                    media_type="tv",
                    disc_scope=disc_scope,
                    season_number=1,
                    episode_range_start=episode,
                    episode_range_end=episode,
                )
                conn.execute(
                    "INSERT INTO job_selected_media (job_id, media_type, tmdb_id, title, season_number, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (job_id, "tv", 67124, "Living Scriptures Animated Stories", 1,
                     "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                )
                for title_id, length in enumerate(minutes):
                    conn.execute(
                        "INSERT INTO rip_titles (job_id, title_id, duration_seconds, chapter_count, source_file, raw_metadata_json) "
                        "VALUES (?,?,?,?,?,?)",
                        (job_id, title_id, length * 60.0, 6, str(root / f"A1_t{title_id:02d}.mkv"), "{}"),
                    )
                conn.commit()
                with patch.object(mapper, "fetch_tmdb_tv_episodes", return_value=EPISODES), \
                     patch.object(mapper, "_collect_early_identity_text", return_value=[]):
                    result = mapper.map_job_episodes(conn, cfg, job_id)
                rows = conn.execute(
                    "SELECT rip_title_id, episode_start, episode_end, episode_titles_json, confidence "
                    "FROM episode_mappings WHERE job_id = ? ORDER BY rip_title_id",
                    (job_id,),
                ).fetchall()
                return result, [dict(r) for r in rows]
            finally:
                conn.close()

    def test_chosen_episode_maps_without_review(self) -> None:
        result, rows = self.run_mapping(minutes=[29.5])
        self.assertFalse(result["needs_review"])
        self.assertEqual((rows[0]["episode_start"], rows[0]["episode_end"]), (8, 8))
        self.assertIn("Helaman", rows[0]["episode_titles_json"])

    def test_longest_title_is_the_episode_even_when_an_extra_comes_first(self) -> None:
        result, rows = self.run_mapping(minutes=[3.2, 29.5])
        self.assertFalse(result["needs_review"])
        self.assertIsNone(rows[0]["episode_start"])
        self.assertEqual(rows[1]["episode_start"], 8)

    def test_episode_missing_from_the_season_is_an_error_not_a_guess(self) -> None:
        with self.assertRaises(MappingError):
            self.run_mapping(minutes=[29.5], episode=40)

    def test_full_season_scope_still_guesses_and_asks(self) -> None:
        # The behaviour single_episode exists to avoid: with nothing but
        # position and duration to go on, any answer is a guess and must ask.
        result, rows = self.run_mapping(minutes=[29.5], disc_scope="full_season", episode=None)
        self.assertTrue(result["needs_review"])
        self.assertNotEqual(rows[0]["episode_start"], 8)


if __name__ == "__main__":
    unittest.main()

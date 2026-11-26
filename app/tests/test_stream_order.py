"""
Blu-ray titles assigned to episodes in the order their video is stored.

The Teen Titans season 2 Blu-ray lists its thirteen episodes as MakeMKV titles
2, 1, 0, 11, 10, 12, 9, 8, 7, 5, 4, 3, 6. Mapping by title order put every one
of them on the wrong episode. The scan below is that disc's: playlist names and
stream numbers as MakeMKV reported them.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.mapper import _rows_in_stream_order  # noqa: E402

# title: (duration, playlist, first stream)
TEEN_TITANS_S2 = {
    0: ("0:21:43", "00003.mpls", "3"),
    1: ("0:21:44", "00002.mpls", "2"),
    2: ("0:21:43", "00001.mpls", "1"),
    3: ("0:22:06", "00013.mpls", "16"),
    4: ("0:21:47", "00006.mpls", "15"),
    5: ("0:22:29", "00012.mpls", "14"),
    6: ("0:22:05", "00007.mpls", "17"),
    7: ("0:22:33", "00005.mpls", "13"),
    8: ("0:21:55", "00104.mpls", "12"),
    9: ("0:22:08", "00103.mpls", "7"),
    10: ("0:22:02", "00101.mpls", "5"),
    11: ("0:21:59", "00004.mpls", "4"),
    12: ("0:21:41", "00102.mpls", "6"),
}
EPISODE_ORDER = [2, 1, 0, 11, 10, 12, 9, 8, 7, 5, 4, 3, 6]


def seconds(clock: str) -> float:
    h, m, s = (int(p) for p in clock.split(":"))
    return h * 3600.0 + m * 60.0 + s


class StreamOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = SimpleNamespace(staging_root=self._tmp.name)
        self.logs = Path(self._tmp.name) / "jobs" / "job-1" / "logs"
        self.logs.mkdir(parents=True)

    def write_scan(self, titles) -> None:
        lines = []
        for title, (duration, source, streams) in titles.items():
            lines.append(f'TINFO:{title},9,0,"{duration}"')
            if source:
                lines.append(f'TINFO:{title},16,0,"{source}"')
            lines.append(f'TINFO:{title},26,0,"{streams}"')
        (self.logs / "makemkv_disc_info.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def rows(self, titles):
        return [
            {
                "id": 100 + title,
                "title_id": title,
                "duration_seconds": seconds(duration),
                "source_file": f"Teen Titans S2_t{title:02d}.mkv",
            }
            for title, (duration, _, _) in titles.items()
        ]

    def order(self, titles, rows=None):
        ordered = _rows_in_stream_order(self.cfg, "job-1", rows or self.rows(titles))
        return None if ordered is None else [r["title_id"] for r in ordered]

    def test_teen_titans_season_2_comes_out_in_episode_order(self) -> None:
        self.write_scan(TEEN_TITANS_S2)
        self.assertEqual(self.order(TEEN_TITANS_S2), EPISODE_ORDER)

    def test_a_disc_already_in_order_is_left_alone(self) -> None:
        in_order = {i: ("0:21:44", f"0100{i + 1}.mpls", str(i)) for i in range(5)}
        self.write_scan(in_order)
        self.assertIsNone(self.order(in_order))

    def test_a_dvd_is_left_alone(self) -> None:
        dvd = {0: ("0:21:44", "", "5"), 1: ("0:21:50", "", "1")}
        self.write_scan(dvd)
        self.assertIsNone(self.order(dvd))

    def test_no_scan_leaves_the_order_alone(self) -> None:
        self.assertIsNone(self.order(TEEN_TITANS_S2))

    def test_a_scan_that_describes_other_titles_is_not_trusted(self) -> None:
        # Title numbers shift when a scan and a rip filter lengths differently.
        self.write_scan(TEEN_TITANS_S2)
        rows = self.rows(TEEN_TITANS_S2)
        rows[3]["duration_seconds"] += 600.0
        self.assertIsNone(self.order(TEEN_TITANS_S2, rows))

    def test_a_short_extra_the_scan_missed_stays_last(self) -> None:
        self.write_scan(TEEN_TITANS_S2)
        rows = self.rows(TEEN_TITANS_S2)
        rows.append({"id": 999, "title_id": 13, "duration_seconds": 298.0, "source_file": "x_t13.mkv"})
        self.assertEqual(self.order(TEEN_TITANS_S2, rows), EPISODE_ORDER + [13])


if __name__ == "__main__":
    unittest.main()

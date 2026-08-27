"""
Tests for MakeMKV robot-output parsing and title selection.

These guard the decisions that determine how much of a disc gets ripped, which
is the difference between a 20-minute and a 60-minute disc.
"""

import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
import unittest.mock
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import autorippr.rip as rip  # noqa: E402
from autorippr.config import AppConfig  # noqa: E402
from autorippr.makemkv import (  # noqa: E402
    TitleCandidate,
    build_title_candidates,
    overall_fraction,
    parse_duration,
    parse_progress_line,
    select_titles,
)


def title(title_id: int, minutes: float, *, size_gb: float = 1.0, name: str = "") -> TitleCandidate:
    return TitleCandidate(
        title_id=title_id,
        duration_seconds=minutes * 60.0,
        size_bytes=int(size_gb * 1024**3),
        chapter_count=6,
        segment_count=1,
        name=name or f"Title {title_id}",
    )


class ParseDurationTests(unittest.TestCase):
    def test_parses_hours_minutes_seconds(self) -> None:
        self.assertEqual(parse_duration("1:23:45"), 5025.0)

    def test_parses_minutes_seconds(self) -> None:
        self.assertEqual(parse_duration("23:45"), 1425.0)

    def test_returns_zero_for_garbage(self) -> None:
        self.assertEqual(parse_duration("not a duration"), 0.0)
        self.assertEqual(parse_duration(""), 0.0)


class ProgressParsingTests(unittest.TestCase):
    def test_parses_progress_values(self) -> None:
        event = parse_progress_line("PRGV:16384,32768,65536")
        assert event is not None
        self.assertEqual(event.kind, "values")
        self.assertEqual(event.current, 16384)
        self.assertEqual(event.total, 32768)
        self.assertAlmostEqual(overall_fraction(event), 0.5)

    def test_parses_current_operation_name(self) -> None:
        event = parse_progress_line('PRGC:5057,0,"Analyzing seamless segments"')
        assert event is not None
        self.assertEqual(event.kind, "current_op")
        self.assertEqual(event.text, "Analyzing seamless segments")

    def test_parses_message_with_embedded_commas(self) -> None:
        event = parse_progress_line(
            'MSG:3007,0,2,"File 00:01, chapter 2 added","%1 added","x"'
        )
        assert event is not None
        self.assertEqual(event.kind, "message")
        self.assertEqual(event.text, "File 00:01, chapter 2 added")

    def test_ignores_unrelated_lines(self) -> None:
        self.assertIsNone(parse_progress_line("Saving 3 titles into directory C:\\out"))
        self.assertIsNone(parse_progress_line(""))

    def test_survives_malformed_progress_line(self) -> None:
        self.assertIsNone(parse_progress_line("PRGV:only,two"))
        self.assertIsNone(parse_progress_line("PRGV:a,b,c"))

    def test_zero_maximum_does_not_divide_by_zero(self) -> None:
        event = parse_progress_line("PRGV:1,2,0")
        assert event is not None
        self.assertEqual(event.maximum, 65536)


class BuildTitleCandidatesTests(unittest.TestCase):
    def test_builds_from_tinfo_fields(self) -> None:
        per_title = {
            0: {
                "fields": {"9": "1:30:00", "8": "12", "11": "4294967296", "2": "Feature"},
                "display_name": "Feature",
            }
        }
        candidates = build_title_candidates(per_title)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].duration_seconds, 5400.0)
        self.assertEqual(candidates[0].chapter_count, 12)
        self.assertEqual(candidates[0].size_bytes, 4294967296)

    def test_missing_fields_do_not_raise(self) -> None:
        candidates = build_title_candidates({3: {"fields": {}}})
        self.assertEqual(candidates[0].title_id, 3)
        self.assertEqual(candidates[0].duration_seconds, 0.0)


class MovieSelectionTests(unittest.TestCase):
    def test_picks_the_feature_and_skips_extras(self) -> None:
        candidates = [
            title(0, 118, name="Feature"),
            title(1, 3, name="Studio logo"),
            title(2, 8, name="Trailer"),
            title(3, 12, name="Making of"),
        ]
        selection = select_titles(candidates, media_type="movie")
        self.assertEqual(selection.title_ids, [0])
        self.assertEqual(len(selection.skipped), 3)
        self.assertFalse(selection.is_everything)

    def test_double_feature_keeps_both_features(self) -> None:
        candidates = [
            title(0, 95, name="Feature A"),
            title(1, 88, name="Feature B"),
            title(2, 4, name="Trailer"),
        ]
        selection = select_titles(candidates, media_type="movie", movie_mode="double_feature")
        self.assertEqual(selection.title_ids, [0, 1])

    def test_bluray_playlist_duplicates_collapse_to_largest(self) -> None:
        """Playlist obfuscation lists the same feature many times."""
        candidates = [
            title(0, 120, size_gb=8.0, name="Playlist 800"),
            title(1, 120, size_gb=30.0, name="Playlist 801"),
            title(2, 120, size_gb=12.0, name="Playlist 802"),
        ]
        selection = select_titles(candidates, media_type="movie")
        self.assertEqual(selection.title_ids, [1])

    def test_single_title_disc_selects_everything(self) -> None:
        selection = select_titles([title(0, 100)], media_type="movie")
        self.assertTrue(selection.is_everything)

    def test_missing_durations_fall_back_to_all(self) -> None:
        candidates = [title(0, 0), title(1, 0)]
        selection = select_titles(candidates, media_type="movie")
        self.assertTrue(selection.is_everything)
        self.assertEqual(selection.title_ids, [0, 1])


class TvSelectionTests(unittest.TestCase):
    def test_keeps_episodes_and_drops_play_all(self) -> None:
        candidates = [
            title(0, 44, name="Episode 1"),
            title(1, 44, name="Episode 2"),
            title(2, 44, name="Episode 3"),
            title(3, 132, name="Play All"),
            title(4, 2, name="Logo"),
        ]
        selection = select_titles(candidates, media_type="tv")
        self.assertEqual(selection.title_ids, [0, 1, 2])
        self.assertTrue(any("play-all" in s for s in selection.skipped))

    def test_keeps_combined_episode_titles(self) -> None:
        """Two episodes in one title must be kept -- the splitter handles them."""
        candidates = [
            title(0, 44, name="Episode 1"),
            title(1, 95, name="Episodes 2-3 combined"),
            title(2, 3, name="Trailer"),
        ]
        selection = select_titles(
            candidates, media_type="tv", min_episode_minutes=10.0, max_episode_minutes=60.0
        )
        self.assertEqual(selection.title_ids, [0, 1])

    def test_all_episodes_means_rip_everything(self) -> None:
        candidates = [title(0, 44), title(1, 44)]
        selection = select_titles(candidates, media_type="tv")
        self.assertTrue(selection.is_everything)

    def test_nothing_matching_window_falls_back_to_all(self) -> None:
        """An unusual disc must not silently produce an empty rip."""
        candidates = [title(0, 5), title(1, 6)]
        selection = select_titles(
            candidates, media_type="tv", min_episode_minutes=20.0, max_episode_minutes=60.0
        )
        self.assertTrue(selection.is_everything)
        self.assertEqual(selection.title_ids, [0, 1])

    def test_empty_disc_returns_no_titles(self) -> None:
        selection = select_titles([], media_type="tv")
        self.assertEqual(selection.title_ids, [])


class SpaceGuardTests(unittest.TestCase):
    """
    MakeMKV reports title sizes during the scan, so a rip that cannot fit is
    knowable before it starts. Finding out an hour in leaves a part-written
    file and a disc to rip over again.
    """

    def _cfg(self, staging: str) -> AppConfig:
        return AppConfig(
            tmdb_api_key="k", makemkv_path="x", ffmpeg_path="x", ffprobe_path="x",
            staging_root=staging, nas_root=staging,
            db_path=staging + "/a.db", log_path=staging + "/a.log",
        )

    def _disc(self, size_bytes: int):
        return {0: {"fields": {"9": "1:30:00", "11": str(size_bytes)}}}

    def test_refuses_a_rip_that_cannot_fit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            usage = type("U", (), {"free": 1 * 1024 ** 3, "total": 100, "used": 1})
            with patch("autorippr.rip.shutil.disk_usage", return_value=usage):
                with self.assertRaises(rip.RipError) as ctx:
                    rip._ensure_space_for_rip(None, cfg, "j", self._disc(8 * 1024 ** 3), None)
            message = str(ctx.exception)
            self.assertIn("Not enough space", message)
            self.assertIn("GB is free", message)

    def test_allows_a_rip_with_room_to_spare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            usage = type("U", (), {"free": 200 * 1024 ** 3, "total": 500, "used": 1})
            with patch("autorippr.rip.shutil.disk_usage", return_value=usage):
                rip._ensure_space_for_rip(None, cfg, "j", self._disc(8 * 1024 ** 3), None)

    def test_only_counts_the_titles_being_ripped(self) -> None:
        """Selecting one feature must not be blocked by the extras' size."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            disc = {
                0: {"fields": {"9": "1:30:00", "11": str(4 * 1024 ** 3)}},
                1: {"fields": {"9": "0:05:00", "11": str(40 * 1024 ** 3)}},
            }
            usage = type("U", (), {"free": 15 * 1024 ** 3, "total": 100, "used": 1})
            with patch("autorippr.rip.shutil.disk_usage", return_value=usage):
                rip._ensure_space_for_rip(None, cfg, "j", disc, [0])
                with self.assertRaises(rip.RipError):
                    rip._ensure_space_for_rip(None, cfg, "j", disc, None)

    def test_unknown_free_space_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            with patch("autorippr.rip.shutil.disk_usage", side_effect=OSError("no")):
                rip._ensure_space_for_rip(None, cfg, "j", self._disc(999 * 1024 ** 3), None)

    def test_missing_disc_info_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rip._ensure_space_for_rip(None, self._cfg(tmp), "j", {}, None)


if __name__ == "__main__":
    unittest.main()


class AlternateTitleTests(unittest.TestCase):
    """
    A worn disc that carries the feature twice must not fail on the bad copy.

    Taken from a real PONYO DVD: two 102.5-minute titles, 444 KB apart in size.
    Selection took the larger one, which sat on scratched sectors, and the job
    died -- while the copy it discarded ripped perfectly.
    """

    def _ponyo(self) -> list[TitleCandidate]:
        return [
            title(0, 102.5, size_gb=6.3577, name="C1_t00.mkv"),
            title(1, 102.5, size_gb=6.3581, name="C1_t01.mkv"),
            title(2, 3.3, name="D1_t02.mkv"),
            title(3, 5.1, name="A1_t03.mkv"),
            title(4, 13.7, name="E1_t04.mkv"),
        ]

    def test_the_discarded_twin_is_kept_as_a_fallback(self) -> None:
        selection = select_titles(self._ponyo(), media_type="movie")
        self.assertEqual(selection.title_ids, [1], "still prefers the larger copy")
        self.assertEqual(selection.alternates, {1: [0]}, "and remembers the one it passed over")

    def test_extras_never_become_fallbacks(self) -> None:
        # Retrying a 102-minute feature with a 3-minute trailer would produce a
        # file that looks like a successful rip and is not the movie.
        selection = select_titles(self._ponyo(), media_type="movie")
        for fallbacks in selection.alternates.values():
            for alternate in fallbacks:
                self.assertGreater(
                    [c for c in self._ponyo() if c.title_id == alternate][0].duration_minutes,
                    60,
                )

    def test_a_disc_with_one_copy_has_no_fallbacks(self) -> None:
        candidates = [title(0, 100, size_gb=5.0), title(1, 4, size_gb=0.2)]
        self.assertEqual(select_titles(candidates, media_type="movie").alternates, {})

    def test_fallbacks_are_ordered_largest_first(self) -> None:
        candidates = [
            title(0, 100, size_gb=5.0),
            title(1, 100, size_gb=5.2),
            title(2, 100, size_gb=5.1),
        ]
        selection = select_titles(candidates, media_type="movie")
        self.assertEqual(selection.title_ids, [1])
        self.assertEqual(selection.alternates[1], [2, 0])


class PartialOutputTests(unittest.TestCase):
    def test_removes_what_the_failed_attempt_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "good.mkv").write_text("keep")
            (out / "truncated.mkv").write_text("discard")
            rip._discard_partial_output(out, keep={"good.mkv"})
            self.assertEqual([p.name for p in out.glob("*.mkv")], ["good.mkv"])

    def test_leaves_titles_ripped_earlier_alone(self) -> None:
        # A multi-title rip that fails on title 3 must not delete titles 1-2.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in ("t01.mkv", "t02.mkv"):
                (out / name).write_text("done")
            rip._discard_partial_output(out, keep={"t01.mkv", "t02.mkv"})
            self.assertEqual(len(list(out.glob("*.mkv"))), 2)


class AlternateRetryTests(unittest.TestCase):
    """The rip-side half: actually re-running the failed title as its twin."""

    def _retry(self, outcomes: list[int], alternates: list[int], out: Path):
        attempts: list[list[str]] = []

        def fake_stream(**kwargs):
            attempts.append(list(kwargs["cmd"]))
            code = outcomes[len(attempts) - 1]
            if code == 0:
                # A rip that succeeds always leaves a file; the retry now
                # checks for one, because MakeMKV exits 0 having saved nothing.
                (out / f"C1_t{kwargs['cmd'][-2]:0>2}.mkv").write_text("data")
            return code

        with patch.object(rip, "_stream_one_makemkv_rip", side_effect=fake_stream), patch.object(
            rip, "_ensure_job_still_ripping"
        ), patch.object(rip, "append_job_log"):
            code = rip._retry_title_with_alternates(
                conn=unittest.mock.MagicMock(),
                job_id="job",
                cmd=["makemkv", "-r", "mkv", "dev:E:", "1", str(out)],
                log_file=io.StringIO(),
                output_dir=out,
                started_at=0.0,
                timeout_seconds=60,
                title_index=1,
                title_count=1,
                selector="1",
                alternates=alternates,
                keep=set(),
            )
        return code, attempts

    def test_a_readable_twin_rescues_the_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code, attempts = self._retry([0], [0], out)
        self.assertEqual(code, 0)
        self.assertEqual(attempts[0][-2], "0", "retried against the alternate title")

    def test_the_broken_half_file_is_not_left_behind(self) -> None:
        # Otherwise the pipeline finds two MKVs and treats the truncated one as
        # a ripped title alongside the good retry.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "C1_t01.mkv").write_text("truncated")
            code, _ = self._retry([0], [0], out)
            self.assertEqual(code, 0)
            names = sorted(path.name for path in out.glob("*.mkv"))
            self.assertEqual(names, ["C1_t00.mkv"], "only the good retry should remain")

    def test_every_twin_is_tried_before_giving_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code, attempts = self._retry([1, 0], [2, 0], out)
        self.assertEqual(code, 0)
        self.assertEqual([a[-2] for a in attempts], ["2", "0"])

    def test_no_twin_means_no_wasted_second_rip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code, attempts = self._retry([], [], out)
        self.assertEqual(code, 1)
        self.assertEqual(attempts, [], "a disc with one copy must fail immediately")

    def test_a_genuinely_unreadable_disc_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code, attempts = self._retry([1, 1], [2, 0], out)
        self.assertEqual(code, 1)
        self.assertEqual(len(attempts), 2)



class ExitCodeZeroFailureTests(unittest.TestCase):
    """
    MakeMKV exits 0 even when it saves nothing.

    From a real BARBIE_A_FASHION_FAIRYTALE disc: eleven read errors,
    "0 titles saved, 1 failed", "Copy complete", and EXIT_CODE: 0. Trusting
    that exit code made the caller report "rip completed but no MKV files were
    found" -- and meant the alternate-title retry, which only runs on failure,
    could never fire on the damaged discs it exists for.
    """

    def _run(self, *, creates_file: bool, title_ids=None, alternates=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            fake_exe = root / "makemkvcon64.exe"
            fake_exe.write_text("")
            calls: list[list[str]] = []

            def fake_stream(**kwargs):
                calls.append(list(kwargs["cmd"]))
                if creates_file:
                    (out / f"C1_t{len(calls):02d}.mkv").write_text("data")
                return 0

            with patch.object(rip, "_resolve_makemkv_cli_path", return_value=fake_exe), patch.object(
                rip, "_stream_one_makemkv_rip", side_effect=fake_stream
            ), patch.object(rip, "_ensure_job_still_ripping"), patch.object(rip, "append_job_log"):
                _, exit_code = rip._run_makemkv_rip_streaming(
                    conn=unittest.mock.MagicMock(),
                    job_id="job",
                    makemkv_path=str(fake_exe),
                    output_dir=out,
                    log_path=root / "makemkv.log",
                    source_spec="dev:E:",
                    timeout_seconds=600,
                    title_ids=title_ids or [1],
                    title_alternates=alternates,
                )
            return exit_code, calls

    def test_saving_nothing_is_a_failure_despite_exit_zero(self) -> None:
        exit_code, _ = self._run(creates_file=False)
        self.assertNotEqual(exit_code, 0)

    def test_a_real_rip_still_succeeds(self) -> None:
        exit_code, _ = self._run(creates_file=True)
        self.assertEqual(exit_code, 0)

    def test_saving_nothing_triggers_the_alternate_retry(self) -> None:
        # The whole point: without this, a damaged disc's twin is never tried.
        _, calls = self._run(creates_file=False, title_ids=[1], alternates={1: [0]})
        self.assertGreaterEqual(len(calls), 2, "the alternate title was never attempted")
        self.assertEqual(calls[1][-2], "0")


class FailureDescriptionTests(unittest.TestCase):
    """The message is the only thing the user sees; it has to name the cause."""

    REAL_LOG = (
        'MSG:2003,0,3,"Error \'Scsi error - MEDIUM ERROR:L-EC UNCORRECTABLE ERROR\' occurred '
        "while reading '/VIDEO_TS/VTS_01_1.VOB' at offset '967847936'\"\n"
        'MSG:2023,131072,3,"Encountered 11 errors of type \'Read Error\' - see '
        'http://www.makemkv.com/errors/dvdread/"\n'
        'MSG:5004,128,2,"0 titles saved, 1 failed"\n'
    )

    def test_read_errors_are_named_and_counted(self) -> None:
        # The old branch required the words "hash check", so this exact log --
        # eleven documented read errors -- fell through to "failed with
        # non-zero exit code", which is both useless and untrue here.
        msg = rip._describe_makemkv_failure(self.REAL_LOG, Path("mk.log"))
        self.assertIn("11", msg)
        self.assertNotIn("non-zero exit code", msg)

    def test_it_tells_the_user_what_to_do(self) -> None:
        msg = rip._describe_makemkv_failure(self.REAL_LOG, Path("mk.log")).lower()
        self.assertIn("clean", msg)
        self.assertIn("centre straight outward", msg)
        self.assertIn("other drive", msg)

    def test_saving_nothing_without_read_errors_is_still_explained(self) -> None:
        msg = rip._describe_makemkv_failure('MSG:5004,128,2,"0 titles saved, 1 failed"', Path("m"))
        self.assertNotIn("non-zero exit code", msg)
        self.assertIn("without saving", msg.lower())

    def test_an_unrecognised_failure_still_points_at_the_log(self) -> None:
        msg = rip._describe_makemkv_failure("MSG:9999,0,0,\"something new\"", Path("mk.log"))
        self.assertIn("mk.log", msg)


class ProgressFailureTests(unittest.TestCase):
    """
    Telemetry must never be able to abort the work it reports on.

    Two jobs ran at once. The first finished and entered mapping, which holds
    the write lock while it writes a season's episodes; the second was twenty
    minutes into a healthy rip, committing a progress heartbeat every second.
    That commit exceeded the busy timeout and raised, which killed the rip,
    orphaned MakeMKV, and left the job stuck in `ripping`.
    """

    def _stream(self, *, progress_raises: bool, status_raises: bool = False):
        import sqlite3 as sq

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            log_path = root / "makemkv.log"

            lines = [
                "PRGV:100,100,65536\n",
                "PRGV:200,200,65536\n",
                'MSG:5011,0,0,"Operation successfully completed"\n',
            ]

            class FakeStdout:
                def __init__(self, rows):
                    self._rows = iter(rows)

                def __iter__(self):
                    return self._rows

                def close(self):
                    pass

            class FakeProc:
                returncode = 0

                def __init__(self):
                    self.stdout = FakeStdout(lines)

                def wait(self):
                    return 0

                def kill(self):
                    pass

            def fake_upsert(*_a, **_k):
                if progress_raises:
                    raise sq.OperationalError("database is locked")

            def fake_still_ripping(*_a, **_k):
                if status_raises:
                    raise sq.OperationalError("database is locked")
                return True

            conn = unittest.mock.MagicMock()
            if progress_raises:
                conn.execute.side_effect = sq.OperationalError("database is locked")

            with patch.object(rip, "upsert_progress", side_effect=fake_upsert), patch.object(
                rip, "_job_is_still_ripping", side_effect=fake_still_ripping
            ), patch.object(rip.subprocess, "Popen", return_value=FakeProc()), patch.object(
                rip, "_output_size_mb", return_value=10.0
            ):
                with open(log_path, "w", encoding="utf-8") as lf:
                    return rip._stream_one_makemkv_rip(
                        conn=conn,
                        job_id="job",
                        cmd=["mk", "mkv", "dev:E:", "0", str(out)],
                        log_file=lf,
                        output_dir=out,
                        # monotonic() is uptime-based, so a literal 0 start
                        # makes any timeout look already exceeded.
                        started_at=time.monotonic(),
                        timeout_seconds=99999,
                        title_index=1,
                        title_count=1,
                    )

    def test_a_locked_database_does_not_kill_the_rip(self) -> None:
        # The whole incident in one assertion: the rip finishes anyway.
        self.assertEqual(self._stream(progress_raises=True), 0)

    def test_a_healthy_rip_is_unaffected(self) -> None:
        self.assertEqual(self._stream(progress_raises=False), 0)


class CancellationCheckTests(unittest.TestCase):
    def test_an_unreadable_database_is_not_a_cancellation(self) -> None:
        # Aborting a healthy rip over a transient lock is the failure mode this
        # avoids; a real cancellation is still there to be seen a second later.
        import sqlite3 as sq

        conn = unittest.mock.MagicMock()
        with patch.object(rip, "get_job", side_effect=sq.OperationalError("database is locked")):
            self.assertTrue(rip._job_is_still_ripping(conn, "job"))

    def test_a_cancelled_job_still_stops_the_rip(self) -> None:
        conn = unittest.mock.MagicMock()
        with patch.object(rip, "get_job", return_value={"status": "error"}):
            self.assertFalse(rip._job_is_still_ripping(conn, "job"))

    def test_a_deleted_job_still_stops_the_rip(self) -> None:
        conn = unittest.mock.MagicMock()
        with patch.object(rip, "get_job", return_value=None):
            self.assertFalse(rip._job_is_still_ripping(conn, "job"))

"""
Running without someone watching.

The first 36 recorded jobs spent 21 hours ripping and 58 hours waiting for a
person: errored until someone pressed Resume, stalled on an unreadable sector
with nothing said, or finished ripping and only then asking which film it was.
These cover what closes that gap -- notifications, the stall watchdog, asking
early, keeping the reason a job failed -- and the scans that no longer repeat.
"""

import dataclasses
import itertools
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import main as cli  # noqa: E402
from autorippr import notify, pipeline, rip  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.rip import RipError  # noqa: E402
from autorippr.state import create_job, get_job, list_job_logs, transition_job  # noqa: E402
from autorippr.tmdb import TmdbError  # noqa: E402
from test_single_episode import build_config  # noqa: E402
from test_title_versions import DISC, disc_info_text  # noqa: E402


class _Receiver(BaseHTTPRequestHandler):
    received: list[dict] = []
    answer = 200

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).received.append({"path": self.path, "json": json.loads(body)})
        self.send_response(type(self).answer)
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass


class WebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = build_config(Path(self._tmp.name))
        _Receiver.received = []
        _Receiver.answer = 200
        self.server = HTTPServer(("127.0.0.1", 0), _Receiver)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/webhook/hook-secret"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def send(self, url: str, **kwargs) -> dict:
        cfg = dataclasses.replace(self.cfg, notify_webhook_url=url)
        return notify.send_notification(cfg, notify.EVENT_FAILED, "Title", "Message", **kwargs)

    def test_posts_json_to_the_webhook(self) -> None:
        result = self.send(self.url, job={"id": "j1", "disc_label": "BARBIE", "status": "error"})
        self.assertTrue(result["sent"])
        (request,) = _Receiver.received
        self.assertEqual(request["path"], "/api/webhook/hook-secret")
        self.assertEqual(request["json"]["event"], "failed")
        self.assertEqual(request["json"]["message"], "Message")
        self.assertEqual(request["json"]["disc_label"], "BARBIE")

    def test_no_url_means_no_notification(self) -> None:
        self.assertFalse(self.send("")["sent"])
        self.assertEqual(_Receiver.received, [])

    def test_only_web_urls_are_opened(self) -> None:
        # urllib would read a local file for this.
        result = self.send("file:///C:/Windows/win.ini")
        self.assertFalse(result["sent"])
        self.assertIn("http", result["detail"])

    def test_a_refusing_webhook_is_reported_not_raised(self) -> None:
        _Receiver.answer = 404
        with self.assertLogs("autorippr.notify", level="WARNING"):
            result = self.send(self.url)
        self.assertFalse(result["sent"])
        self.assertIn("404", result["detail"])

    def test_an_unreachable_webhook_never_leaks_the_url(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        with self.assertLogs("autorippr.notify", level="WARNING") as captured:
            result = self.send(self.url)
        self.assertFalse(result["sent"])
        self.assertNotIn("hook-secret", result["detail"])
        self.assertNotIn("hook-secret", " ".join(captured.output))


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = build_config(self.root)
        self.conn = open_db(self.cfg.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def movie(self, status: str = "ripping", **kwargs) -> str:
        job_id = create_job(self.conn, disc_label="HARRY_POTTER_2", media_type="movie", **kwargs)
        if status != "queued":
            transition_job(self.conn, job_id, status)
        self.conn.commit()
        return job_id

    def messages(self, job_id: str) -> list[str]:
        return [str(row["message"]) for row in list_job_logs(self.conn, job_id)]


class PipelineNotificationTests(_DbCase):
    def run_pipeline(self, job_id: str, outcome):
        effect = {"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome}
        with patch.object(pipeline, "_run_pipeline_for_job", **effect), patch.object(
            pipeline, "send_notification"
        ) as send:
            try:
                pipeline.run_pipeline_for_job(self.conn, self.cfg, job_id)
            except RipError:
                pass
        return [call.args[1] for call in send.call_args_list], send

    def test_finishing_is_announced(self) -> None:
        events, _ = self.run_pipeline(self.movie(), {"status": "done"})
        self.assertEqual(events, [notify.EVENT_FINISHED])

    def test_a_job_that_stops_to_ask_is_announced(self) -> None:
        events, send = self.run_pipeline(self.movie(), {"status": "ripping", "needs_review": True})
        self.assertEqual(events, [notify.EVENT_NEEDS_ATTENTION])
        self.assertIn("version", send.call_args.args[3])

    def test_a_job_already_waiting_is_not_announced_again(self) -> None:
        job_id = self.movie()
        self.conn.execute("UPDATE jobs SET awaiting_review = 1 WHERE id = ?", (job_id,))
        self.conn.commit()
        events, _ = self.run_pipeline(job_id, {"status": "ripping", "needs_review": True})
        self.assertEqual(events, [])

    def test_a_failure_is_announced_without_the_log_path(self) -> None:
        job_id = self.movie()
        transition_job(self.conn, job_id, "error", r"MakeMKV hit 11 unreadable sector(s). See log: C:\x\makemkv.log")
        events, send = self.run_pipeline(job_id, RipError("boom"))
        self.assertEqual(events, [notify.EVENT_FAILED])
        self.assertIn("unreadable", send.call_args.args[3])
        self.assertNotIn("makemkv.log", send.call_args.args[3])

    def test_cancelling_by_hand_is_not_announced(self) -> None:
        job_id = self.movie()
        transition_job(self.conn, job_id, "error", "Cancelled by user.")
        events, _ = self.run_pipeline(job_id, RipError("Rip cancelled or no longer active"))
        self.assertEqual(events, [])

    def test_a_broken_notifier_cannot_fail_the_job(self) -> None:
        job_id = self.movie()
        with patch.object(pipeline, "_run_pipeline_for_job", return_value={"status": "done"}), patch.object(
            pipeline, "send_notification", side_effect=RuntimeError("notifier exploded")
        ), self.assertLogs("autorippr.pipeline", level="ERROR"):
            self.assertEqual(pipeline.run_pipeline_for_job(self.conn, self.cfg, job_id), {"status": "done"})


class FailureReasonTests(_DbCase):
    def test_the_reason_survives_the_retry(self) -> None:
        job_id = self.movie()
        transition_job(self.conn, job_id, "error", "MakeMKV hit 11 unreadable sector(s)")
        transition_job(self.conn, job_id, "queued")  # the retry clears jobs.error_message
        self.assertIsNone(get_job(self.conn, job_id)["error_message"])
        self.assertIn("Failed: MakeMKV hit 11 unreadable sector(s)", self.messages(job_id))


class EarlyIdentifyTests(_DbCase):
    def identify(self, job_id: str, outcome):
        effect = {"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome}
        with patch.object(pipeline, "identify_job_with_tmdb", **effect) as ident, patch.object(
            pipeline, "send_notification"
        ) as send:
            pipeline._identify_movie_early(self.conn, self.cfg, job_id)
        return ident.call_count, send.call_count

    def test_an_unplaceable_movie_is_asked_about_as_the_rip_starts(self) -> None:
        self.assertEqual(self.identify(self.movie(), {"needs_review": True}), (1, 1))

    def test_a_confident_match_asks_nothing(self) -> None:
        self.assertEqual(self.identify(self.movie(), {"needs_review": False}), (1, 0))

    def test_tmdb_being_down_does_not_stop_the_rip(self) -> None:
        job_id = self.movie()
        self.assertEqual(self.identify(job_id, TmdbError("TMDB request failed: timed out")), (1, 0))
        self.assertTrue(any("identified after the rip" in m for m in self.messages(job_id)))

    def test_tv_and_multi_movie_discs_are_left_alone(self) -> None:
        tv = create_job(self.conn, disc_label="SHOW_S1_D1", media_type="tv")
        pack = self.movie(movie_mode="double_feature")
        self.assertEqual(self.identify(tv, {"needs_review": True}), (0, 0))
        self.assertEqual(self.identify(pack, {"needs_review": True}), (0, 0))

    def test_a_resumed_rip_does_not_ask_twice(self) -> None:
        job_id = self.movie()
        self.conn.execute(
            "INSERT INTO tmdb_candidates (job_id, tmdb_id, media_type, title, score) VALUES (?, 672, 'movie', 'X', 0.5)",
            (job_id,),
        )
        self.conn.commit()
        self.assertEqual(self.identify(job_id, {"needs_review": True}), (0, 0))

    def review(self, *, status: str, selected=None) -> dict:
        (self.root / "jobs" / "j" / "logs").mkdir(parents=True, exist_ok=True)
        return cli._build_review_state(
            cfg=SimpleNamespace(staging_root=str(self.root)),
            job={"id": "j", "status": status, "media_type": "movie", "movie_mode": "single"},
            logs=[{"message": "TMDB candidates need manual review", "timestamp": None}],
            progress_row=None,
            selected_media=selected,
            selected_movies=[],
            tmdb_rows=[],
            mapping_rows=[],
            rip_title_rows=[],
            bundle_association=None,
            tmdb_threshold=0.75,
        )["tmdb"]

    def test_the_choice_is_offered_while_ripping(self) -> None:
        tmdb = self.review(status="ripping")
        self.assertTrue(tmdb["needed"])
        self.assertTrue(tmdb["early"])

    def test_the_offer_goes_away_once_answered(self) -> None:
        tmdb = self.review(status="ripping", selected={"tmdb_id": 672})
        self.assertFalse(tmdb["needed"])

    def test_after_the_rip_it_is_an_ordinary_review(self) -> None:
        tmdb = self.review(status="identifying")
        self.assertTrue(tmdb["needed"])
        self.assertFalse(tmdb["early"])


class _Stdout:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def close(self) -> None:
        pass


class StallWatchdogTests(unittest.TestCase):
    """A fake clock ticks one second per reading, so a stall takes no real time."""

    def stream(self, *, sizes, stall_timeout: float, endless: bool):
        killed = threading.Event()

        def stuck_lines():
            # MakeMKV on a bad sector: still talking, going nowhere.
            while not killed.is_set():
                yield "PRGV:32058,32058,65536\n"

        class Proc:
            stdout = _Stdout(stuck_lines() if endless else ["PRGV:100,100,65536\n"] * 40)

            def kill(self) -> None:
                killed.set()

            def wait(self) -> int:
                return 1 if killed.is_set() else 0

        clock = itertools.count(1000.0, 1.0)
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "makemkv.log"
            with patch.object(rip.subprocess, "Popen", return_value=Proc()), patch.object(
                rip.time, "monotonic", side_effect=lambda: next(clock)
            ), patch.object(rip, "_job_is_still_ripping", return_value=True), patch.object(
                rip, "upsert_progress"
            ), patch.object(rip, "_output_size_mb", side_effect=sizes), open(
                log_path, "w", encoding="utf-8"
            ) as lf:
                exit_code = rip._stream_one_makemkv_rip(
                    conn=MagicMock(),
                    job_id="job",
                    cmd=["mk"],
                    log_file=lf,
                    output_dir=Path(tmp),
                    started_at=1000.0,
                    timeout_seconds=10**9,
                    title_index=1,
                    title_count=1,
                    stall_timeout_seconds=stall_timeout,
                )
            return exit_code, log_path.read_text(encoding="utf-8")

    def test_a_rip_writing_nothing_is_stopped(self) -> None:
        exit_code, log_text = self.stream(sizes=itertools.repeat(11904.0), stall_timeout=60, endless=True)
        self.assertEqual(exit_code, rip.MAKEMKV_STALLED)
        self.assertIn(rip.STALL_NOTE, log_text)

    def test_a_rip_that_keeps_writing_is_left_alone(self) -> None:
        exit_code, log_text = self.stream(sizes=itertools.count(1.0, 5.0), stall_timeout=3, endless=False)
        self.assertEqual(exit_code, 0)
        self.assertNotIn(rip.STALL_NOTE, log_text)

    def test_zero_turns_the_watchdog_off(self) -> None:
        exit_code, _ = self.stream(sizes=itertools.repeat(11904.0), stall_timeout=0, endless=False)
        self.assertEqual(exit_code, 0)

    def test_the_failure_says_what_a_stall_means(self) -> None:
        log_text = (
            "MSG:2003,0,3,\"Error 'Scsi error - MEDIUM ERROR:UNRECOVERED READ ERROR' occurred\"\n" * 3
            + f"\n{rip.STALL_NOTE} 900s (11,904 MB written); MakeMKV was stopped.\n"
        )
        message = rip._describe_makemkv_failure(log_text, Path("makemkv.log"))
        self.assertIn("no progress for 15 minutes", message)
        self.assertIn("11,904 MB", message)
        self.assertIn("3 read errors", message)


class AttemptLogTests(unittest.TestCase):
    def test_each_attempts_log_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "makemkv.log"
            for attempt in range(1, 4):
                log_path.write_text(f"attempt {attempt}", encoding="utf-8")
                rip._archive_attempt_log(log_path)
            self.assertFalse(log_path.exists())
            self.assertEqual((Path(tmp) / "makemkv.attempt-2.log").read_text(encoding="utf-8"), "attempt 2")

    def test_old_attempts_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "makemkv.log"
            for attempt in range(1, 10):
                log_path.write_text(f"attempt {attempt}", encoding="utf-8")
                rip._archive_attempt_log(log_path)
            kept = sorted(p.name for p in Path(tmp).glob("makemkv.attempt-*.log"))
            self.assertEqual(len(kept), rip.ATTEMPT_LOGS_KEPT)
            self.assertIn("makemkv.attempt-9.log", kept)


class DiscScanReuseTests(_DbCase):
    def setUp(self) -> None:
        super().setUp()
        self.job_id = self.movie()
        self.log_dir = self.root / "jobs" / self.job_id / "logs"
        self.log_dir.mkdir(parents=True)
        self.serial = 0x1234ABCD

    def info(self, *, cfg=None, drive: str = "D:"):
        text = disc_info_text(DISC)
        drives = [{"drive": "D:", "has_media": True, "volume_label": "HP2", "volume_serial": self.serial}]
        with patch.object(rip, "discover_optical_drives", return_value=drives), patch.object(
            rip, "_scan_disc_with_retries", return_value=(text, rip._parse_makemkv_info_output(text))
        ) as scan:
            result = rip._disc_info_for_rip(self.conn, cfg or self.cfg, self.job_id, "dev:D:", drive, self.log_dir)
        return result, scan.call_count

    def test_a_retry_with_the_same_disc_does_not_scan_again(self) -> None:
        first, scans = self.info()
        self.assertEqual(scans, 1)
        again, scans = self.info()
        self.assertEqual(scans, 0)
        self.assertEqual(sorted(again), sorted(first))

    def test_a_different_disc_is_scanned(self) -> None:
        self.info()
        self.serial = 0x0BADF00D
        self.assertEqual(self.info()[1], 1)

    def test_a_different_minimum_length_is_scanned(self) -> None:
        # Title numbers shift with --minlength, so the old scan's ids are wrong.
        self.info()
        self.assertEqual(self.info(cfg=dataclasses.replace(self.cfg, rip_min_title_seconds=600))[1], 1)

    def test_a_scan_the_rip_contradicted_is_not_reused(self) -> None:
        self.info()
        rip._forget_disc_scan(self.log_dir)
        self.assertEqual(self.info()[1], 1)

    def single_episode(self, **kwargs) -> dict:
        job = {"media_type": "tv", "disc_scope": "single_episode", "episode_range_start": 5}
        job.update(kwargs)
        return job

    def test_a_single_episode_dvd_skips_the_scan(self) -> None:
        (self.root / "VIDEO_TS").mkdir()
        self.assertTrue(rip._separate_scan_is_wasted(self.single_episode(), str(self.root)))

    def test_the_scan_still_runs_where_its_answer_matters(self) -> None:
        dvd = str(self.root)
        (self.root / "VIDEO_TS").mkdir()
        self.assertFalse(rip._separate_scan_is_wasted(self.single_episode(disc_scope="full_season"), dvd))
        self.assertFalse(rip._separate_scan_is_wasted(self.single_episode(episode_range_start=None), dvd))
        self.assertFalse(rip._separate_scan_is_wasted(self.single_episode(media_type="movie"), dvd))
        # A Blu-ray has no VIDEO_TS, and "everything" on one is tens of GB.
        (self.root / "VIDEO_TS").rmdir()
        self.assertFalse(rip._separate_scan_is_wasted(self.single_episode(), dvd))


if __name__ == "__main__":
    unittest.main()

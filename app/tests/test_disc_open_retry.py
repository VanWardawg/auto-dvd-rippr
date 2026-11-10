"""
A drive that fails to open the disc on the first try.

The slim USB Blu-ray drive answers MakeMKV's first reads of a freshly loaded
disc with "MEDIUM ERROR:NO SEEK COMPLETE" and MakeMKV reports "Failed to open
disc". The same disc opens a minute later, but the job used to go straight to
error and sit there until someone pressed Resume.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr import rip  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.state import create_job, transition_job  # noqa: E402
from test_single_episode import build_config  # noqa: E402


COULD_NOT_OPEN = (
    'MSG:2003,0,3,"Error \'Scsi error - MEDIUM ERROR:NO SEEK COMPLETE\' occurred while reading"\n'
    'DRV:0,2,999,0,"BD-RE MATSHITA BD-MLT UJ272","","D:"\n'
    'MSG:5010,0,0,"Failed to open disc","Failed to open disc"\n'
    "TCOUNT:0\n"
)
OPENED = 'TCOUNT:1\nTINFO:0,9,0,"1:32:00"\n'


class DiscOpenRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = build_config(self.root)
        self.conn = open_db(self.cfg.db_path)
        self.job_id = create_job(self.conn, disc_label="PAW Patrol The Movie", media_type="movie")
        transition_job(self.conn, self.job_id, "ripping")
        self.conn.commit()
        patcher = patch.object(rip, "DISC_OPEN_RETRY_DELAY_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def scan(self, *results):
        with patch.object(rip, "_read_makemkv_disc_info", side_effect=list(results)) as read:
            outcome = rip._scan_disc_with_retries(self.conn, self.cfg, self.job_id, "dev:D:", "D:")
        return outcome, read.call_count

    def test_scan_retries_until_the_disc_opens(self) -> None:
        (text, titles), calls = self.scan((COULD_NOT_OPEN, {}), (OPENED, {0: {}}))
        self.assertEqual(calls, 2)
        self.assertEqual(titles, {0: {}})

    def test_a_timed_out_scan_is_retried(self) -> None:
        (_, titles), calls = self.scan(("", {}), (OPENED, {0: {}}))
        self.assertEqual(calls, 2)
        self.assertEqual(titles, {0: {}})

    def test_scan_gives_up_after_the_last_attempt(self) -> None:
        (text, _), calls = self.scan(*[(COULD_NOT_OPEN, {})] * rip.DISC_OPEN_ATTEMPTS)
        self.assertEqual(calls, rip.DISC_OPEN_ATTEMPTS)
        self.assertTrue(rip._could_not_open_disc(text))

    def test_an_opened_disc_with_no_titles_is_not_retried(self) -> None:
        _, calls = self.scan(("TCOUNT:0\n", {}))
        self.assertEqual(calls, 1)

    def rip_attempts(self, *results, leave_file=False, auto_retries=0, drive_retries=2):
        out = self.root / "rip_output"
        out.mkdir(exist_ok=True)

        remaining = list(results)

        def fake_rip(**kwargs):
            if leave_file:
                (out / "title_t00.mkv").write_bytes(b"x")
            return remaining.pop(0)

        with patch.object(rip, "_run_makemkv_rip_streaming", side_effect=fake_rip) as run, patch.object(
            rip, "_wait_for_disc"
        ) as wait:
            outcome = rip._rip_with_retries(
                conn=self.conn,
                job_id=self.job_id,
                output_dir=out,
                auto_retries=auto_retries,
                drive_retries=drive_retries,
                drive_retry_seconds=120.0,
            )
        self.waits = [call.args[4] for call in wait.call_args_list]
        return outcome, run.call_count

    def test_rip_retries_an_open_failure(self) -> None:
        (_, exit_code), calls = self.rip_attempts((COULD_NOT_OPEN, 11), ("saved", 0))
        self.assertEqual((exit_code, calls), (0, 2))

    def test_rip_does_not_retry_other_failures(self) -> None:
        (_, exit_code), calls = self.rip_attempts(("Encountered 11 errors of type 'Read Error'", 1))
        self.assertEqual((exit_code, calls), (1, 1))

    def test_rip_does_not_retry_once_something_was_saved(self) -> None:
        (_, exit_code), calls = self.rip_attempts((COULD_NOT_OPEN, 11), leave_file=True)
        self.assertEqual((exit_code, calls), (11, 1))

    READ_ERRORS = "Encountered 11 errors of type 'Read Error'"
    # Captain America: Civil War, 27 minutes into its second attempt.
    DROPPED_OUT = (
        "MSG:2003,0,3,\"Error 'Scsi error - NOT READY:MEDIUM NOT PRESENT - TRAY OPEN' occurred while "
        "reading '/BDMV/STREAM/00300.m2ts' at offset '15136063488'\"\n"
        'MSG:5037,516,2,"Copy complete. 0 titles saved, 1 failed."\n'
    )

    def test_a_drive_that_lost_the_disc_is_retried_past_the_ordinary_limit(self) -> None:
        # One early dropout used to spend the only retry there was.
        (_, exit_code), calls = self.rip_attempts(
            (self.DROPPED_OUT, rip.MAKEMKV_SAVED_NOTHING),
            (self.DROPPED_OUT, rip.MAKEMKV_SAVED_NOTHING),
            ("saved", 0),
            auto_retries=1,
            drive_retries=8,
        )
        self.assertEqual((exit_code, calls), (0, 3))

    def test_each_drive_retry_waits_longer_up_to_a_ceiling(self) -> None:
        self.rip_attempts(*[(self.DROPPED_OUT, 1)] * 7, drive_retries=6)
        self.assertEqual(self.waits, [120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0])

    def test_drive_retries_are_limited(self) -> None:
        (_, exit_code), calls = self.rip_attempts(*[(self.DROPPED_OUT, 1)] * 3, auto_retries=5, drive_retries=2)
        self.assertEqual((exit_code, calls), (1, 3))

    def test_the_failure_names_the_drive_not_the_disc(self) -> None:
        message = rip._describe_makemkv_failure(self.DROPPED_OUT, Path("makemkv.log"))
        self.assertIn("drive stopped seeing the disc", message)
        self.assertNotIn("damaged", message)

    def wait_for_disc(self, *presences, delay=0.0):
        disc = {"volume_serial": 1, "volume_label": "CIVIL_WAR"}
        with patch.object(rip, "DRIVE_POLL_SECONDS", 0), patch.object(
            rip, "_disc_presence", side_effect=list(presences)
        ) as presence:
            rip._wait_for_disc(self.conn, self.job_id, "D:", disc, delay)
        return presence.call_count

    def test_an_open_tray_is_waited_out(self) -> None:
        self.assertEqual(self.wait_for_disc("absent", "absent", "same"), 3)

    def test_a_different_disc_stops_the_retries(self) -> None:
        with self.assertRaises(rip.RipError) as raised:
            self.wait_for_disc("absent", "different")
        self.assertIn("different disc", str(raised.exception))

    def test_a_disc_that_never_comes_back_is_given_up_on(self) -> None:
        with patch.object(rip, "DRIVE_WAIT_FOR_DISC_SECONDS", 0), self.assertRaises(rip.RipError) as raised:
            self.wait_for_disc("absent", "absent", "absent")
        self.assertIn("has not shown it again", str(raised.exception))

    def test_the_wait_can_be_cancelled(self) -> None:
        transition_job(self.conn, self.job_id, "error")
        with self.assertRaises(rip.RipError):
            self.wait_for_disc("absent", delay=600.0)

    def test_the_wait_shows_as_a_wait(self) -> None:
        self.wait_for_disc("same")
        row = self.conn.execute("SELECT kind, detail FROM job_progress WHERE job_id = ?", (self.job_id,)).fetchone()
        self.assertEqual(row["kind"], "waiting")

    def presence(self, drives, disc={"volume_serial": 1, "volume_label": "CIVIL_WAR"}):
        with patch.object(rip, "discover_optical_drives", return_value=drives):
            return rip._disc_presence("D:", disc)

    def test_the_disc_is_recognised_by_serial_and_label(self) -> None:
        drive = {"drive": "D:", "has_media": True, "volume_serial": 1, "volume_label": "CIVIL_WAR"}
        self.assertEqual(self.presence([drive]), "same")
        self.assertEqual(self.presence([dict(drive, volume_serial=2, volume_label="WONKA")]), "different")
        self.assertEqual(self.presence([dict(drive, has_media=False)]), "absent")
        # The whole drive dropping off USB is the same fault, not a reason to stop.
        self.assertEqual(self.presence([{"drive": "E:", "has_media": True}]), "absent")

    def test_a_rip_that_failed_partway_is_started_again(self) -> None:
        (_, exit_code), calls = self.rip_attempts((self.READ_ERRORS, 1), ("saved", 0), auto_retries=1)
        self.assertEqual((exit_code, calls), (0, 2))

    def test_the_failed_attempt_is_cleared_before_the_retry(self) -> None:
        (self.root / "rip_output").mkdir()
        (self.root / "rip_output" / "half_t00.mkv").write_bytes(b"x")
        with patch.object(rip, "_run_makemkv_rip_streaming", side_effect=[(self.READ_ERRORS, 1), ("saved", 0)]):
            rip._rip_with_retries(
                conn=self.conn, job_id=self.job_id, output_dir=self.root / "rip_output", auto_retries=1
            )
        self.assertEqual(list((self.root / "rip_output").glob("*.mkv")), [])

    def test_automatic_retries_are_limited(self) -> None:
        (_, exit_code), calls = self.rip_attempts(*[(self.READ_ERRORS, 1)] * 2, auto_retries=1)
        self.assertEqual((exit_code, calls), (1, 2))

    def test_a_stalled_rip_is_not_started_again(self) -> None:
        stalled = f"{rip.STALL_NOTE} 900s (11,904 MB written); MakeMKV was stopped."
        (_, exit_code), calls = self.rip_attempts((stalled, rip.MAKEMKV_STALLED), auto_retries=3)
        self.assertEqual((exit_code, calls), (rip.MAKEMKV_STALLED, 1))

    def test_an_expired_key_is_not_retried(self) -> None:
        (_, _), calls = self.rip_attempts(("This application version is too old.", 1), auto_retries=3)
        self.assertEqual(calls, 1)

    def test_cancelling_during_the_wait_stops_the_retries(self) -> None:
        transition_job(self.conn, self.job_id, "error")
        self.conn.commit()
        with self.assertRaises(rip.RipError):
            self.scan((COULD_NOT_OPEN, {}), (OPENED, {0: {}}))


if __name__ == "__main__":
    unittest.main()

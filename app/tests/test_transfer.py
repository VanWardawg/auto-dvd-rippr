"""
Tests for NAS transfer: checksums and destination availability.

Two behaviours matter here. The recorded checksum must actually describe the
content that was copied -- before, it was computed by reading the destination
back and was never compared to anything, so it could not detect corruption.
And an unreachable NAS must be reported clearly and early rather than
surfacing as a raw WinError from a mkdir deep inside the copy loop.
"""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.config import AppConfig  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.state import create_job  # noqa: E402
from autorippr.transfer import (  # noqa: E402
    TransferError,
    _copy_with_retry,
    ensure_nas_available,
    transfer_job_outputs,
)

PAYLOAD = b"auto-ripper transfer payload " * 5000


def build_config(root: Path, **overrides) -> AppConfig:
    values = dict(
        tmdb_api_key="test-key",
        makemkv_path=r"C:\mk.exe",
        ffmpeg_path=r"C:\ffmpeg.exe",
        ffprobe_path=r"C:\ffprobe.exe",
        staging_root=str(root),
        nas_root=str(root / "nas"),
        db_path=str(root / "autorippr.db"),
        log_path=str(root / "autorippr.log"),
    )
    values.update(overrides)
    return AppConfig(**values)


class ChecksumTests(unittest.TestCase):
    def _copy(self, tmp: Path, verify: bool):
        tmp.mkdir(parents=True, exist_ok=True)
        source = tmp / "source.mkv"
        source.write_bytes(PAYLOAD)
        dest = tmp / "dest.mkv"
        temp_dest = dest.with_suffix(dest.suffix + ".part")
        conn = open_db(str(tmp / "a.db"))
        try:
            job_id = create_job(conn, disc_label="DISC")
            conn.commit()
            return _copy_with_retry(
                conn=conn,
                job_id=job_id,
                output_id=1,
                source=source,
                temp_dest=temp_dest,
                final_dest=dest,
                retries=1,
                backoff_seconds=1,
                verify=verify,
            ), dest
        finally:
            conn.close()

    def test_checksum_matches_the_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (ok, checksum, err), dest = self._copy(Path(tmp), verify=False)
            self.assertTrue(ok, err)
            self.assertEqual(checksum, hashlib.sha256(PAYLOAD).hexdigest())
            self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_checksum_is_identical_with_and_without_verification(self) -> None:
        """Verification changes the checking, never the recorded digest."""
        with tempfile.TemporaryDirectory() as tmp:
            (_, without, _), _ = self._copy(Path(tmp) / "a", verify=False)
        with tempfile.TemporaryDirectory() as tmp:
            (_, with_verify, _), _ = self._copy(Path(tmp) / "b", verify=True)
        self.assertEqual(without, with_verify)

    def test_fast_path_does_not_read_the_file_back(self) -> None:
        """The whole point: no second pass over the network by default."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch("autorippr.transfer._sha256") as read_back:
                (ok, _, err), _ = self._copy(Path(tmp), verify=False)
            self.assertTrue(ok, err)
            read_back.assert_not_called()

    def test_verify_reads_the_file_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_hash = hashlib.sha256(PAYLOAD).hexdigest()
            with patch("autorippr.transfer._sha256", return_value=source_hash) as read_back:
                (ok, _, err), _ = self._copy(Path(tmp), verify=True)
            self.assertTrue(ok, err)
            read_back.assert_called_once()

    def test_verify_detects_a_corrupted_destination(self) -> None:
        """A mismatch must fail the copy, not be silently recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch("autorippr.transfer._sha256", return_value="0" * 64):
                (ok, checksum, err), dest = self._copy(Path(tmp), verify=True)
            self.assertFalse(ok)
            self.assertIsNone(checksum)
            self.assertIn("checksum_mismatch", str(err))
            # A failed copy must not leave a file behind as if it succeeded.
            self.assertFalse(dest.exists())


class NasAvailabilityTests(unittest.TestCase):
    def test_missing_nas_root_is_reported_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_config(Path(tmp), nas_root=str(Path(tmp) / "not-mounted"))
            with self.assertRaises(TransferError) as ctx:
                ensure_nas_available(cfg)
            message = str(ctx.exception)
            self.assertIn("not reachable", message)
            self.assertIn("resume", message.lower())

    def test_blank_nas_root_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_config(Path(tmp), nas_root="   ")
            with self.assertRaises(TransferError) as ctx:
                ensure_nas_available(cfg)
            self.assertIn("No NAS root", str(ctx.exception))

    def test_file_where_a_folder_should_be(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "nas-is-a-file"
            bogus.write_text("x", encoding="utf-8")
            cfg = build_config(Path(tmp), nas_root=str(bogus))
            with self.assertRaises(TransferError) as ctx:
                ensure_nas_available(cfg)
            self.assertIn("not a folder", str(ctx.exception))

    def test_reachable_nas_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nas").mkdir()
            ensure_nas_available(build_config(root))

    def test_transfer_checks_before_touching_outputs(self) -> None:
        """The failure must arrive before the copy loop, not from a mkdir."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = build_config(root, nas_root=str(root / "gone"))
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="DISC", media_type="movie")
                conn.execute(
                    "INSERT INTO outputs (job_id, local_path, transfer_status) VALUES (?,?,?)",
                    (job_id, str(root / "movie.mkv"), "pending"),
                )
                conn.commit()
                with self.assertRaises(TransferError) as ctx:
                    transfer_job_outputs(conn, cfg, job_id)
                self.assertIn("not reachable", str(ctx.exception))
                # Nothing should have been attempted against the outputs.
                attempts = conn.execute(
                    "SELECT transfer_attempts FROM outputs WHERE job_id = ?", (job_id,)
                ).fetchone()["transfer_attempts"]
                self.assertEqual(attempts, 0)
            finally:
                conn.close()


class ExistingDestinationTests(unittest.TestCase):
    """
    An existing NAS destination is only a conflict when the contents differ.

    Re-ripping the I Heart Minnie disc re-produced two episodes an earlier
    disc had already banked. The transfer refused both -- correctly declining
    to overwrite -- but recorded them as errors, which errored the whole job
    even though its three genuinely new episodes had copied fine. Identical
    bytes under the identical name are not a conflict; they are the transfer's
    goal already met. A destination with *different* content stays an error,
    because choosing which file survives is a human's call.
    """

    def _run_transfer(self, *, nas_payload: bytes | None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = build_config(root)
        (root / "nas").mkdir()
        conn = open_db(cfg.db_path)
        self.addCleanup(conn.close)

        job_id = create_job(conn, disc_label="MIH-0N-NW1.1_DES", media_type="tv")
        finalized = root / "jobs" / job_id / "finalized" / "Show (2006)" / "Season 01"
        finalized.mkdir(parents=True)
        local = finalized / "Show (2006) - s01e08 - Minnie's Birthday.mkv"
        local.write_bytes(PAYLOAD)

        nas_file = (
            root / "nas" / "TVShows" / "Show (2006)" / "Season 01"
            / "Show (2006) - s01e08 - Minnie's Birthday.mkv"
        )
        if nas_payload is not None:
            nas_file.parent.mkdir(parents=True)
            nas_file.write_bytes(nas_payload)

        conn.execute(
            "INSERT INTO outputs (job_id, local_path, transfer_status) VALUES (?,?,?)",
            (job_id, str(local), "pending"),
        )
        conn.commit()
        result = transfer_job_outputs(conn, cfg, job_id)
        row = conn.execute(
            "SELECT transfer_status, checksum_sha256, last_error FROM outputs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return result, row, nas_file

    def test_an_identical_existing_file_counts_as_transferred(self) -> None:
        result, row, _ = self._run_transfer(nas_payload=PAYLOAD)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["copied"]), 1)
        self.assertTrue(result["copied"][0]["already_present"])
        self.assertEqual(row["transfer_status"], "done")

    def test_the_recorded_checksum_describes_the_content(self) -> None:
        _, row, _ = self._run_transfer(nas_payload=PAYLOAD)
        self.assertEqual(row["checksum_sha256"], hashlib.sha256(PAYLOAD).hexdigest())

    def test_different_content_is_still_refused(self) -> None:
        # The half of the behaviour that must not soften: a name collision
        # with different bytes means something is misidentified somewhere.
        result, row, _ = self._run_transfer(nas_payload=b"a different episode entirely")
        self.assertEqual(result["copied"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("destination_exists", result["errors"][0]["error"])
        self.assertEqual(row["transfer_status"], "error")

    def test_the_existing_file_is_never_modified(self) -> None:
        # Refusing to overwrite is the invariant; the dedupe must not dent it.
        for payload in (PAYLOAD, b"a different episode entirely"):
            with self.subTest(identical=payload == PAYLOAD):
                _, _, nas_file = self._run_transfer(nas_payload=payload)
                self.assertEqual(nas_file.read_bytes(), payload)

    def test_a_missing_destination_still_copies_normally(self) -> None:
        result, row, nas_file = self._run_transfer(nas_payload=None)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["copied"]), 1)
        self.assertNotIn("already_present", result["copied"][0])
        self.assertEqual(row["transfer_status"], "done")
        self.assertEqual(nas_file.read_bytes(), PAYLOAD)


if __name__ == "__main__":
    unittest.main()

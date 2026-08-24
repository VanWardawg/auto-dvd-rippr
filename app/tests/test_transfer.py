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


if __name__ == "__main__":
    unittest.main()

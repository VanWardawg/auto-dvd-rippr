import sys
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import main as cli_main  # noqa: E402
from autorippr.config import AppConfig  # noqa: E402
from autorippr.db import open_db  # noqa: E402
from autorippr.pipeline import run_pipeline_for_job  # noqa: E402
from autorippr.rip import (  # noqa: E402
    RipError,
    _build_makemkv_source_spec,
    _clear_stale_rip_output,
    _describe_makemkv_failure,
    _drives_with_media_from_log,
    _ensure_drive_available,
    _no_disc_message,
    _parse_beta_expiry_date,
)
from autorippr.state import create_job, get_job  # noqa: E402
from autorippr.transfer import TransferError, transfer_job_outputs  # noqa: E402


def build_test_config(staging_root: str, db_path: str, log_path: str) -> AppConfig:
    return AppConfig(
        tmdb_api_key="test-key",
        makemkv_path=r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
        ffmpeg_path=r"C:\tools\ffmpeg\bin\ffmpeg.exe",
        ffprobe_path=r"C:\tools\ffmpeg\bin\ffprobe.exe",
        staging_root=staging_root,
        nas_root=r"Z:\\",
        db_path=db_path,
        log_path=log_path,
    )



def _rip_timestamps(quiet_seconds: float) -> tuple[str, str]:
    """
    A live rip's (updated_at, last_advance_at), exactly `quiet_seconds` apart.

    Both come off one clock reading on purpose. Taking two separate readings
    makes the real gap `quiet_seconds` minus however long elapsed between them,
    which put a 60s gap under the 60s threshold often enough to fail the suite
    roughly one run in fifty -- measured at up to 7ms of drift.
    """
    now = datetime.now(timezone.utc)
    return now.isoformat(), (now - timedelta(seconds=quiet_seconds)).isoformat()

class RuntimeRegressionTests(unittest.TestCase):
    def test_pipeline_moves_job_to_error_when_rip_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = build_test_config(
                staging_root=str(tmp_path),
                db_path=str(tmp_path / "autorippr.db"),
                log_path=str(tmp_path / "autorippr.log"),
            )
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="LUCA", media_type="movie")

                with (
                    patch("autorippr.pipeline.recover_completed_rip", return_value=None),
                    patch("autorippr.pipeline.execute_rip_job", side_effect=RipError("rip exploded")),
                ):
                    with self.assertRaises(RipError):
                        run_pipeline_for_job(conn, cfg, job_id)

                updated = get_job(conn, job_id)
                self.assertIsNotNone(updated)
                self.assertEqual(updated["status"], "error")
                self.assertEqual(updated["current_stage"], "error")
                self.assertIn("rip exploded", updated["error_message"])
            finally:
                conn.close()

    def test_build_review_state_flags_stalled_rip(self) -> None:
        """A rip that keeps heartbeating without advancing must be flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_id = "job-123"
            log_dir = tmp_path / "jobs" / job_id / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "makemkv.log").write_text("", encoding="utf-8")
            cfg = SimpleNamespace(staging_root=str(tmp_path))
            job = {
                "id": job_id,
                "status": "ripping",
                "media_type": "movie",
                "movie_mode": "single",
                "updated_at": "2026-08-15T02:08:04+00:00",
            }
            # Still reporting a minute after progress last moved.
            _stalled = _rip_timestamps(60)
            progress_row = {
                "stage": "ripping",
                "current_units": 1024.0,
                "total_units": 65536.0,
                "updated_at": _stalled[0],
                "last_advance_at": _stalled[1],
            }

            review = cli_main._build_review_state(
                cfg=cfg,
                job=job,
                logs=[],
                progress_row=progress_row,
                selected_media=None,
                selected_movies=[],
                tmdb_rows=[],
                mapping_rows=[],
                rip_title_rows=[],
                bundle_association=None,
                tmdb_threshold=0.75,
            )

            self.assertTrue(review["rip"]["needed"])
            self.assertIn("stalled", review["rip"]["reason"].lower())
            self.assertTrue(any("60s" in detail for detail in review["rip"]["details"]))

    def test_build_review_state_accepts_healthy_rip(self) -> None:
        """A rip that is advancing must not be flagged for review."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_id = "job-124"
            log_dir = tmp_path / "jobs" / job_id / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "makemkv.log").write_text("", encoding="utf-8")
            cfg = SimpleNamespace(staging_root=str(tmp_path))
            job = {
                "id": job_id,
                "status": "ripping",
                "media_type": "movie",
                "movie_mode": "single",
                "updated_at": "2026-08-15T02:08:04+00:00",
            }
            _healthy = _rip_timestamps(1)
            progress_row = {
                "stage": "ripping",
                "current_units": 32768.0,
                "total_units": 65536.0,
                "updated_at": _healthy[0],
                "last_advance_at": _healthy[1],
            }

            review = cli_main._build_review_state(
                cfg=cfg,
                job=job,
                logs=[],
                progress_row=progress_row,
                selected_media=None,
                selected_movies=[],
                tmdb_rows=[],
                mapping_rows=[],
                rip_title_rows=[],
                bundle_association=None,
                tmdb_threshold=0.75,
            )

            self.assertFalse(review["rip"]["needed"])

    def test_empty_drive_is_rejected_before_ripping(self) -> None:
        """A job aimed at a drive with no disc must fail fast, not after a scan."""
        drives = [
            {"drive": "E:", "has_media": False, "volume_label": ""},
            {"drive": "F:", "has_media": True, "volume_label": "BARBIE"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = open_db(str(tmp_path / "autorippr.db"))
            try:
                job_id = create_job(conn, disc_label="x", optical_drive="E:")
                conn.commit()
                with patch("autorippr.rip.discover_optical_drives", return_value=drives):
                    with self.assertRaises(RipError) as ctx:
                        _ensure_drive_available(conn, job_id, "E:")
                message = str(ctx.exception)
                self.assertIn("No disc detected in E:", message)
                # The whole point: say where the disc actually is.
                self.assertIn("F:", message)
                self.assertIn("BARBIE", message)
            finally:
                conn.close()

    def test_loaded_drive_passes_the_guard(self) -> None:
        drives = [{"drive": "F:", "has_media": True, "volume_label": "BARBIE"}]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = open_db(str(tmp_path / "autorippr.db"))
            try:
                job_id = create_job(conn, disc_label="x", optical_drive="F:")
                conn.commit()
                with patch("autorippr.rip.discover_optical_drives", return_value=drives):
                    _ensure_drive_available(conn, job_id, "F:")
            finally:
                conn.close()

    def test_undetectable_drives_do_not_block_the_rip(self) -> None:
        """If drives cannot be enumerated, let MakeMKV decide."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = open_db(str(tmp_path / "autorippr.db"))
            try:
                job_id = create_job(conn, disc_label="x", optical_drive="E:")
                conn.commit()
                with patch("autorippr.rip.discover_optical_drives", return_value=[]):
                    _ensure_drive_available(conn, job_id, "E:")
            finally:
                conn.close()

    def test_unknown_drive_letter_is_reported(self) -> None:
        drives = [{"drive": "E:", "has_media": True, "volume_label": "DISC"}]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = open_db(str(tmp_path / "autorippr.db"))
            try:
                job_id = create_job(conn, disc_label="x", optical_drive="Q:")
                conn.commit()
                with patch("autorippr.rip.discover_optical_drives", return_value=drives):
                    with self.assertRaises(RipError) as ctx:
                        _ensure_drive_available(conn, job_id, "Q:")
                self.assertIn("was not found", str(ctx.exception))
            finally:
                conn.close()

    def test_failed_to_open_disc_names_the_drive_holding_a_disc(self) -> None:
        """MakeMKV's DRV: lines say where the disc is -- surface that."""
        log_text = "\n".join(
            [
                'DRV:0,0,999,0,"DVD+R-DL ASUS DRW-24F1ST","","E:"',
                'DRV:1,2,999,1,"BD-RE MATSHITA BD-MLT UJ272","BARBIE","F:"',
                'MSG:5010,0,0,"Failed to open disc","Failed to open disc"',
            ]
        )
        message = _describe_makemkv_failure(log_text, Path("makemkv.log"))
        self.assertIn("could not open the disc", message.lower())
        self.assertIn("F:", message)
        self.assertIn("BARBIE", message)

    def test_failed_to_open_disc_without_any_loaded_drive(self) -> None:
        log_text = "\n".join(
            [
                'DRV:0,0,999,0,"DVD+R-DL ASUS DRW-24F1ST","","E:"',
                'MSG:5010,0,0,"Failed to open disc","Failed to open disc"',
            ]
        )
        message = _describe_makemkv_failure(log_text, Path("makemkv.log"))
        self.assertIn("could not open the disc", message.lower())
        self.assertNotIn("select that drive", message.lower())

    def test_drive_parsing_ignores_empty_slots(self) -> None:
        log_text = "\n".join(
            [
                'DRV:0,0,999,0,"DVD+R-DL ASUS","","E:"',
                'DRV:2,256,999,0,"","",""',
                'DRV:1,2,999,1,"BD-RE MATSHITA","BARBIE","F:"',
            ]
        )
        self.assertEqual(_drives_with_media_from_log(log_text), [("F:", "BARBIE")])

    def test_expired_beta_key_message_still_takes_priority(self) -> None:
        log_text = 'MSG:5021,0,0,"The temporary key has expired","x"'
        message = _describe_makemkv_failure(log_text, Path("makemkv.log"))
        self.assertIn("beta key", message.lower())

    def test_no_disc_message_without_alternatives(self) -> None:
        message = _no_disc_message("E:", [{"drive": "E:", "has_media": False, "volume_label": ""}])
        self.assertIn("No disc detected in E:", message)
        self.assertIn("Insert a disc", message)

    def test_parse_beta_expiry_date_handles_end_of_month(self) -> None:
        parsed = _parse_beta_expiry_date("The current beta key is valid until end of September 2026.")
        self.assertEqual(str(parsed), "2026-09-30")

    def test_clear_stale_rip_output_removes_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rip_dir = Path(tmp) / "rip_output"
            rip_dir.mkdir(parents=True, exist_ok=True)
            (rip_dir / "A1_t00.mkv").write_bytes(b"partial")
            (rip_dir / "A1_t00.tmp").write_bytes(b"partial")
            removed = _clear_stale_rip_output(rip_dir)
            self.assertEqual(removed, 2)
            self.assertEqual(list(rip_dir.iterdir()), [])

    def test_build_makemkv_source_spec_prefers_selected_drive(self) -> None:
        self.assertEqual(_build_makemkv_source_spec("E:", 0), "dev:E:")
        self.assertEqual(_build_makemkv_source_spec(None, 1), "disc:1")

    def test_same_drive_conflict_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = open_db(str(tmp_path / "autorippr.db"))
            try:
                active_job = create_job(conn, disc_label="Disc A", optical_drive="E:", media_type="movie")
                other_job = create_job(conn, disc_label="Disc B", optical_drive="E:", media_type="movie")
                conn.execute(
                    "UPDATE jobs SET status = 'ripping', current_stage = 'ripping' WHERE id = ?",
                    (active_job,),
                )
                conn.commit()

                with self.assertRaises(RipError):
                    _ensure_drive_available(conn, other_job, "E:")
            finally:
                conn.close()

    def test_build_review_state_flags_overwrite_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_id = "job-overwrite"
            log_dir = tmp_path / "jobs" / job_id / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "makemkv.log").write_text(
                'MSG:5001,776,1,"File C:\\\\temp\\\\A1_t00.mkv already exist. Do you want to overwrite?"\n',
                encoding="utf-8",
            )
            cfg = SimpleNamespace(staging_root=str(tmp_path))
            job = {
                "id": job_id,
                "status": "ripping",
                "media_type": "movie",
                "movie_mode": "single",
                "updated_at": "2026-08-17T16:53:20+00:00",
            }
            review = cli_main._build_review_state(
                cfg=cfg,
                job=job,
                logs=[],
                progress_row=None,
                selected_media=None,
                selected_movies=[],
                tmdb_rows=[],
                mapping_rows=[],
                rip_title_rows=[],
                bundle_association=None,
                tmdb_threshold=0.75,
            )
            self.assertTrue(review["rip"]["needed"])
            self.assertIn("overwrite prompt", review["rip"]["reason"].lower())

    def test_pipeline_moves_job_to_error_when_transfer_returns_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = build_test_config(
                staging_root=str(tmp_path),
                db_path=str(tmp_path / "autorippr.db"),
                log_path=str(tmp_path / "autorippr.log"),
            )
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="LUCA", media_type="movie")
                conn.execute(
                    "UPDATE jobs SET status = 'copying', current_stage = 'copying' WHERE id = ?",
                    (job_id,),
                )
                conn.commit()

                with patch(
                    "autorippr.pipeline.transfer_job_outputs",
                    return_value={"job_id": job_id, "copied": [], "errors": [{"output_id": 1, "error": "network_unavailable"}]},
                ):
                    result = run_pipeline_for_job(conn, cfg, job_id)

                updated = get_job(conn, job_id)
                self.assertEqual(result["status"], "error")
                self.assertEqual(updated["status"], "error")
                self.assertIn("network_unavailable", updated["error_message"])
            finally:
                conn.close()

    def test_transfer_job_outputs_raises_transfer_error_when_nas_root_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = build_test_config(
                staging_root=str(tmp_path),
                db_path=str(tmp_path / "autorippr.db"),
                log_path=str(tmp_path / "autorippr.log"),
            )
            conn = open_db(cfg.db_path)
            try:
                job_id = create_job(conn, disc_label="LUCA", media_type="movie")
                finalize_root = tmp_path / "jobs" / job_id / "finalized" / "Luca (2021)"
                finalize_root.mkdir(parents=True, exist_ok=True)
                local_file = finalize_root / "Luca (2021).mkv"
                local_file.write_bytes(b"test")
                conn.execute(
                    """
                    INSERT INTO outputs (job_id, local_path, transfer_status)
                    VALUES (?, ?, 'pending')
                    """,
                    (job_id, str(local_file)),
                )
                conn.commit()
                bad_cfg = AppConfig(
                    tmdb_api_key=cfg.tmdb_api_key,
                    makemkv_path=cfg.makemkv_path,
                    ffmpeg_path=cfg.ffmpeg_path,
                    ffprobe_path=cfg.ffprobe_path,
                    staging_root=cfg.staging_root,
                    nas_root=r"Z:\\",
                    db_path=cfg.db_path,
                    log_path=cfg.log_path,
                )

                with patch("pathlib.Path.mkdir", side_effect=FileNotFoundError("missing share")):
                    with self.assertRaises(TransferError):
                        transfer_job_outputs(conn, bad_cfg, job_id)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

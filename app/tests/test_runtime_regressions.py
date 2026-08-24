import sys
import tempfile
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
    _ensure_drive_available,
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

    def test_build_review_state_flags_stalled_rip_with_flat_heartbeats(self) -> None:
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
            logs = [
                {
                    "timestamp": "2026-08-15T02:07:04+00:00",
                    "message": "Starting MakeMKV rip to C:\\temp\\rip_output",
                },
                {
                    "timestamp": "2026-08-15T02:07:19+00:00",
                    "message": "Rip heartbeat: files=0, size_mb=0.0",
                },
                {
                    "timestamp": "2026-08-15T02:07:34+00:00",
                    "message": "Rip heartbeat: files=0, size_mb=0.0",
                },
                {
                    "timestamp": "2026-08-15T02:07:49+00:00",
                    "message": "Rip heartbeat: files=0, size_mb=0.0",
                },
                {
                    "timestamp": "2026-08-15T02:08:04+00:00",
                    "message": "Rip heartbeat: files=0, size_mb=0.0",
                },
            ]

            review = cli_main._build_review_state(
                cfg=cfg,
                job=job,
                logs=logs,
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
            self.assertTrue(any("0.0 MB" in detail for detail in review["rip"]["details"]))

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

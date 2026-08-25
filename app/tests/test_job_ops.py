"""
Tests for job deletion.

Deleting a job has to clear every table that references it. Because foreign
keys are enforced, missing one produces a bare "FOREIGN KEY constraint failed"
on the final DELETE with no indication of which table was missed -- so these
tests check the invariant against the live schema rather than a fixed list,
and will fail automatically when a new referencing table is added and not
handled.
"""

import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.db import open_db  # noqa: E402
from autorippr.job_ops import (  # noqa: E402
    JobDeleteError,
    _tables_referencing_jobs,
    clear_job_local_artifacts,
    delete_job,
    local_artifact_bytes,
    purge_local_files,
    reclaim_completed_jobs,
    summarize_reclaimable,
)
from autorippr.progress import upsert_progress  # noqa: E402
from autorippr.state import append_job_log, create_job  # noqa: E402


def tables_with_job_fk(conn) -> dict[str, str]:
    """Read the schema directly -- deliberately not reusing the module's helper."""
    found = {}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        table = str(row["name"])
        if table == "jobs":
            continue
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            if str(fk["table"]).lower() == "jobs":
                found[table] = str(fk["from"])
                break
    return found


class DeleteJobTests(unittest.TestCase):
    def test_every_referencing_table_is_covered(self) -> None:
        """The delete list must match the schema, not a stale hardcoded list."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(str(Path(tmp) / "autorippr.db"))
            try:
                from_schema = tables_with_job_fk(conn)
                from_module = dict(_tables_referencing_jobs(conn))
                self.assertEqual(
                    set(from_schema),
                    set(from_module),
                    "delete_job does not cover every table with a foreign key to jobs",
                )
                self.assertEqual(from_schema, from_module, "wrong foreign key column")
            finally:
                conn.close()

    def test_delete_removes_job_with_rows_in_every_child_table(self) -> None:
        """Populate every referencing table, then delete -- must not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = open_db(str(tmp_path / "autorippr.db"))
            try:
                job_id = create_job(conn, disc_label="DISC", media_type="movie")
                append_job_log(conn, job_id, "INFO", "hello", None, None)
                upsert_progress(conn, job_id, stage="ripping", current_units=1.0, total_units=2.0)

                now = "2026-08-24T00:00:00+00:00"
                conn.execute(
                    "INSERT INTO rip_titles (job_id, title_id, duration_seconds, source_file) VALUES (?,?,?,?)",
                    (job_id, 0, 120.0, "t00.mkv"),
                )
                conn.execute(
                    "INSERT INTO tmdb_candidates (job_id, tmdb_id, media_type, title) VALUES (?,?,?,?)",
                    (job_id, 1, "movie", "Movie"),
                )
                conn.execute(
                    "INSERT INTO episode_mappings (job_id, season_number, episode_start) VALUES (?,?,?)",
                    (job_id, 1, 1),
                )
                conn.execute(
                    "INSERT INTO split_plans (job_id, source_file, segment_index) VALUES (?,?,?)",
                    (job_id, "t00.mkv", 0),
                )
                conn.execute(
                    "INSERT INTO outputs (job_id, local_path) VALUES (?,?)",
                    (job_id, str(tmp_path / "out.mkv")),
                )
                output_id = conn.execute(
                    "SELECT id FROM outputs WHERE job_id = ?", (job_id,)
                ).fetchone()["id"]
                conn.execute(
                    "INSERT INTO transfer_attempts (output_id, attempt_number, status, created_at) VALUES (?,?,?,?)",
                    (output_id, 1, "success", now),
                )
                conn.execute(
                    "INSERT INTO job_selected_media (job_id, media_type, tmdb_id, title, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (job_id, "movie", 1, "Movie", now, now),
                )
                conn.execute(
                    "INSERT INTO job_selected_movies (job_id, slot_index, tmdb_id, title, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (job_id, 0, 1, "Movie", now, now),
                )
                conn.execute(
                    "INSERT INTO finalized_manifests (job_id, manifest_json, created_at) VALUES (?,?,?)",
                    (job_id, "{}", now),
                )
                conn.commit()

                # Every referencing table must actually have a row, or this
                # test would pass without exercising the thing it guards.
                for table, column in tables_with_job_fk(conn).items():
                    count = conn.execute(
                        f"SELECT COUNT(*) AS c FROM {table} WHERE {column} = ?", (job_id,)
                    ).fetchone()["c"]
                    self.assertGreater(count, 0, f"{table} was not populated by this test")

                staging_root = tmp_path / "staging"
                (staging_root / "jobs" / job_id).mkdir(parents=True, exist_ok=True)
                (staging_root / "jobs" / job_id / "note.txt").write_text("x", encoding="utf-8")

                result = delete_job(conn, str(staging_root), job_id)

                self.assertEqual(result["deleted_counts"]["jobs"], 1)
                self.assertTrue(result["removed_job_dir"])
                self.assertFalse((staging_root / "jobs" / job_id).exists())

                self.assertIsNone(
                    conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
                )
                for table, column in tables_with_job_fk(conn).items():
                    remaining = conn.execute(
                        f"SELECT COUNT(*) AS c FROM {table} WHERE {column} = ?", (job_id,)
                    ).fetchone()["c"]
                    self.assertEqual(remaining, 0, f"{table} still has rows for the deleted job")
            finally:
                conn.close()

    def test_delete_clears_progress_row(self) -> None:
        """The regression that broke deletion: job_progress held a reference."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = open_db(str(tmp_path / "autorippr.db"))
            try:
                job_id = create_job(conn, disc_label="DISC")
                upsert_progress(conn, job_id, stage="ripping", current_units=5.0)
                conn.commit()
                delete_job(conn, str(tmp_path / "staging"), job_id)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) AS c FROM job_progress").fetchone()["c"], 0
                )
            finally:
                conn.close()

    def test_deleting_unknown_job_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = open_db(str(tmp_path / "autorippr.db"))
            try:
                with self.assertRaises(JobDeleteError):
                    delete_job(conn, str(tmp_path), "no-such-job")
            finally:
                conn.close()


class LocalStorageTests(unittest.TestCase):
    """
    Staging space is the binding constraint when working through a collection,
    so the amount a job holds must be reported accurately and reclaiming it
    must not take the provenance with it.
    """

    def _job_with_files(self, root: Path, conn):
        job_id = create_job(conn, disc_label="D", media_type="movie")
        conn.commit()
        job_dir = root / "jobs" / job_id
        (job_dir / "rip_output").mkdir(parents=True)
        (job_dir / "finalized").mkdir(parents=True)
        (job_dir / "logs").mkdir(parents=True)
        (job_dir / "rip_output" / "t00.mkv").write_bytes(b"x" * 4_000_000)
        (job_dir / "finalized" / "movie.mkv").write_bytes(b"y" * 2_000_000)
        (job_dir / "logs" / "makemkv.log").write_text("log", encoding="utf-8")
        return job_id, job_dir

    def test_reports_the_size_of_staged_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = open_db(str(root / "a.db"))
            try:
                job_id, _ = self._job_with_files(root, conn)
                self.assertEqual(local_artifact_bytes(str(root), job_id), 6_000_000)
            finally:
                conn.close()

    def test_size_is_zero_for_a_job_with_nothing_staged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = open_db(str(root / "a.db"))
            try:
                job_id = create_job(conn, disc_label="D")
                conn.commit()
                self.assertEqual(local_artifact_bytes(str(root), job_id), 0)
            finally:
                conn.close()

    def test_purge_frees_files_but_keeps_the_nas_record(self) -> None:
        """The bytes are reproducible from the disc; the provenance is not."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = open_db(str(root / "a.db"))
            try:
                job_id, job_dir = self._job_with_files(root, conn)
                conn.execute(
                    "INSERT INTO outputs (job_id, local_path, nas_path, checksum_sha256, transfer_status) "
                    "VALUES (?,?,?,?,?)",
                    (job_id, "x.mkv", r"Y:\Movies\movie.mkv", "abc", "done"),
                )
                conn.commit()

                result = purge_local_files(str(root), job_id)

                self.assertEqual(result["freed_bytes"], 6_000_000)
                self.assertFalse((job_dir / "rip_output").exists())
                self.assertFalse((job_dir / "finalized").exists())
                # Logs are small and are the record of what happened.
                self.assertTrue((job_dir / "logs" / "makemkv.log").exists())
                row = conn.execute(
                    "SELECT nas_path, checksum_sha256 FROM outputs WHERE job_id = ?", (job_id,)
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["checksum_sha256"], "abc")
            finally:
                conn.close()

    def test_manual_clear_reports_what_it_freed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = open_db(str(root / "a.db"))
            try:
                job_id, _ = self._job_with_files(root, conn)
                result = clear_job_local_artifacts(conn, str(root), job_id)
                self.assertEqual(result["freed_bytes"], 6_000_000)
                self.assertEqual(local_artifact_bytes(str(root), job_id), 0)
            finally:
                conn.close()

    def test_purging_twice_is_harmless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = open_db(str(root / "a.db"))
            try:
                job_id, _ = self._job_with_files(root, conn)
                purge_local_files(str(root), job_id)
                again = purge_local_files(str(root), job_id)
                self.assertEqual(again["freed_bytes"], 0)
                self.assertEqual(again["removed_paths"], [])
            finally:
                conn.close()


class BulkReclaimTests(unittest.TestCase):
    """
    Clearing 170 jobs one at a time is the same problem at the scale where the
    disk actually fills up -- but only finished work is safe to clear.
    """

    def _job(self, root: Path, conn, status: str, size: int):
        job_id = create_job(conn, disc_label=f"D-{status}", media_type="movie")
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        conn.commit()
        rip = root / "jobs" / job_id / "rip_output"
        rip.mkdir(parents=True)
        (rip / "t00.mkv").write_bytes(b"x" * size)
        return job_id

    def test_only_completed_jobs_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = open_db(str(root / "a.db"))
            try:
                self._job(root, conn, "done", 1_000_000)
                self._job(root, conn, "done", 2_000_000)
                self._job(root, conn, "ripping", 4_000_000)
                self._job(root, conn, "error", 8_000_000)

                summary = summarize_reclaimable(conn, str(root))

                self.assertEqual(summary["job_count"], 2)
                self.assertEqual(summary["total_bytes"], 3_000_000)
            finally:
                conn.close()

    def test_reclaim_frees_completed_and_spares_the_rest(self) -> None:
        """An errored job may be one Resume away; clearing it forces a re-rip."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = open_db(str(root / "a.db"))
            try:
                done_id = self._job(root, conn, "done", 3_000_000)
                ripping_id = self._job(root, conn, "ripping", 4_000_000)
                error_id = self._job(root, conn, "error", 5_000_000)

                result = reclaim_completed_jobs(conn, str(root))

                self.assertEqual(result["freed_bytes"], 3_000_000)
                self.assertEqual(result["job_count"], 1)
                self.assertEqual(local_artifact_bytes(str(root), done_id), 0)
                self.assertEqual(local_artifact_bytes(str(root), ripping_id), 4_000_000)
                self.assertEqual(local_artifact_bytes(str(root), error_id), 5_000_000)
            finally:
                conn.close()

    def test_reclaim_with_nothing_to_free_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = open_db(str(root / "a.db"))
            try:
                result = reclaim_completed_jobs(conn, str(root))
                self.assertEqual(result["freed_bytes"], 0)
                self.assertEqual(result["job_count"], 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

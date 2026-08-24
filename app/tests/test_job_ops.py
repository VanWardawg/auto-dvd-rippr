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
    delete_job,
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


if __name__ == "__main__":
    unittest.main()

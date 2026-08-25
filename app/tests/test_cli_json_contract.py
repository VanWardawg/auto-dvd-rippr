"""
The JSON the CLI prints is a typed contract with the Rust layer.

Rust deserializes job rows into structs with real types. SQLite has no boolean
type, so a column like `awaiting_review` comes back as 0 or 1, and passing that
through unchanged makes serde reject the whole payload with
"invalid type: integer `0`, expected a boolean". The failure is total and
silent-looking: the job list simply does not load, and the UI shows no jobs.

Nothing else in the toolchain can catch this. tsc does not know what Rust
declares, cargo does not know what Python prints, and check-commands.mjs only
verifies that command *names* line up.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from autorippr.db import open_db  # noqa: E402
from autorippr.state import create_job, set_awaiting_review  # noqa: E402

# Fields the Rust JobSummary struct declares as booleans.
BOOLEAN_JOB_FIELDS = ("awaiting_review", "has_local_artifacts")


def run_cli(config_path: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(APP_ROOT / "main.py"), "--config", str(config_path), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        raise AssertionError(f"CLI failed: {result.stderr[-800:]}")
    return result.stdout


class JobJsonContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.config_path = root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "tmdb_api_key": "test-key",
                    "makemkv_path": "x",
                    "ffmpeg_path": "x",
                    "ffprobe_path": "x",
                    "staging_root": str(root / "staging"),
                    "nas_root": str(root / "nas"),
                    "db_path": str(root / "staging" / "autorippr.db"),
                    "log_path": str(root / "staging" / "autorippr.log"),
                }
            ),
            encoding="utf-8",
        )
        conn = open_db(str(root / "staging" / "autorippr.db"))
        try:
            self.job_id = create_job(conn, disc_label="DISC", media_type="movie")
            conn.commit()
            # Exercise both states: the bug only showed on one of them.
            set_awaiting_review(conn, self.job_id, True)
            self.waiting_id = self.job_id
            self.other_id = create_job(conn, disc_label="OTHER", media_type="movie")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_job_list_emits_real_booleans(self) -> None:
        payload = json.loads(run_cli(self.config_path, "job", "list"))
        self.assertTrue(payload["jobs"], "expected at least one job")
        for job in payload["jobs"]:
            for field in BOOLEAN_JOB_FIELDS:
                self.assertIn(field, job)
                self.assertIsInstance(
                    job[field],
                    bool,
                    f"{field} is {type(job[field]).__name__} ({job[field]!r}); "
                    "Rust declares it as a boolean and serde will reject an integer",
                )

    def test_both_true_and_false_survive_as_booleans(self) -> None:
        payload = json.loads(run_cli(self.config_path, "job", "list"))
        by_id = {job["id"]: job for job in payload["jobs"]}
        self.assertIs(by_id[self.waiting_id]["awaiting_review"], True)
        self.assertIs(by_id[self.other_id]["awaiting_review"], False)

    def test_job_snapshot_emits_real_booleans(self) -> None:
        payload = json.loads(run_cli(self.config_path, "job", "snapshot", self.waiting_id))
        self.assertIsInstance(payload["job"]["awaiting_review"], bool)

    def test_job_show_emits_real_booleans(self) -> None:
        payload = json.loads(run_cli(self.config_path, "job", "show", self.waiting_id))
        self.assertIsInstance(payload["job"]["awaiting_review"], bool)

    def test_commands_the_ui_calls_emit_parseable_json(self) -> None:
        """Rust parses these with serde_json, so stdout must be JSON alone."""
        for args in (("job", "list"), ("job", "reclaimable"), ("rip", "drives")):
            with self.subTest(command=" ".join(args)):
                out = run_cli(self.config_path, *args)
                try:
                    json.loads(out)
                except json.JSONDecodeError as exc:
                    self.fail(f"`{' '.join(args)}` did not print pure JSON: {exc}\n{out[:300]}")


if __name__ == "__main__":
    unittest.main()

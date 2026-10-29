"""
One running pipeline per job.

The UI's Resume button spawns a fresh `pipeline run`, and nothing stopped it
doing so while the first one was still alive -- the stall warning invites
exactly that while a slow drive is scanning. Two pipelines then drove the
same drive and wrote the same title_t00.mkv, and the job was finished with a
3-minute extra in place of the episode.

The lock is an OS file lock, not a row in the database, because the process
that holds it can die without cleaning up: the OS drops the lock with the
handle, so a crashed run never blocks the next one.
"""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class JobAlreadyRunningError(RuntimeError):
    pass


@contextmanager
def job_run_lock(staging_root: str | Path, job_id: str) -> Iterator[None]:
    lock_path = Path(staging_root) / "jobs" / job_id / "pipeline.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        try:
            _lock(handle)
        except OSError as exc:
            raise JobAlreadyRunningError(f"A pipeline is already running for job {job_id}.") from exc
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


if os.name == "nt":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

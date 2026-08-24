# Progress and roadmap

Living status document. Update it in the same commit as the work it describes
so any session can pick up mid-stream.

**Last updated:** 2026-08-23

---

## Recently completed

Branch: `feat/hardening-and-rip-improvements` (not yet merged to `main`)

### Track 1 — Repo hygiene and packaging  ✅ complete

- [x] Harden `.gitignore` (secrets, runtime state, build output)
- [x] Untrack `app/config.json` so a real TMDB key cannot leak
- [x] Remove scaffolding clutter (`test.txt`, `create_dirs.py`, `verify_dirs.py`)
- [x] Add `requirements.txt` / `requirements-dev.txt` with a pinned PyInstaller
- [x] Add root `README.md`
- [x] Add `CLAUDE.md` and this file
- [x] Run the test suite in CI
- [x] Single-source the version from the git tag
- [x] Drop the legacy tkinter GUI and exclude Tcl/Tk from the bundle

### Track 2 — Ripping quality (highest user-facing value)  ✅ complete

- [x] Select which titles to rip instead of `makemkvcon mkv ... all`
- [x] Give the Blu-ray disc scan its own, longer timeout
- [x] Parse MakeMKV robot progress (`PRGV`/`PRGC`/`PRGT`) instead of polling directory size
- [x] Eject the disc when a rip finishes

### Track 3 — Concurrency and state  ✅ complete

- [x] Enable SQLite WAL + a longer busy timeout
- [x] Stop running schema DDL on every CLI invocation
- [x] Move progress into a `job_progress` table instead of parsing log strings

---

## Where the time actually goes (measured over 153 completed jobs)

| | total | median | note |
| --- | --- | --- | --- |
| **Waiting for a human** | **107.4h** | 35 min | 37% of jobs stop to ask |
| Ripping | 47.9h | 17.1 min | real compute |
| Copying | 9.9h | 2.0 min | ~halved by streaming checksums |
| Menu analysis | 2.5h | 1.1 min | succeeds 17% of the time |

The machine works ~48h and waits on a human ~107h. Throughput work should
target the waiting, not the compute.

## Next up (found by running the app on real discs)

- **Free the drive during review.** A job awaiting review keeps its disc in the
  drive, so continuous mode cannot start the next one: 17 min of ripping
  occupies the drive for ~52 min. Ejecting after the rip is roughly a 3x
  throughput gain. Blocked on one thing: `_discover_dvd_menu_vobs` reads
  VIDEO_TS off the optical drive, and `eject_after_rip` currently fires before
  identify -- so enabling it today silently disables the menu-analysis fallback
  that succeeds 17% of the time. Fix by capturing menu artifacts during the rip
  stage, then ejecting.
- **Strip disc-junk tokens from labels.** `PRINCESS_BRIDE_CE`, `EVERAFTER169`
  (16:9), `PAW_PATROL_NA`, `THESECRETLIFEOFWALTERMITTY` (no spaces).
- **Batch review queue** so several discs can be cleared in one sitting.
- **Cap menu analysis runtime.** Median is 1.1 min but one job burned 11 min
  for nothing. A 3 min ceiling costs nothing.

- **Clear the progress row when artifacts are cleared.**
  `clear_job_local_artifacts` and `clear_job_output_artifacts` leave the
  `job_progress` row behind, so the UI can briefly show stale rip progress.
  Cosmetic.
- **`run-tauri.mjs` is broken.** It spawns a bare `tauri` from `~/.cargo/bin`,
  which is not installed; `npm run tauri -- dev` fails. Use `npx tauri dev`
  until it falls back to the local CLI in devDependencies.

## Not started

- **Content Security Policy.** `tauri.conf.json` sets `"csp": null`. A
  restrictive policy is easy to write but needs a packaged-app run to confirm
  it does not blank the window, so it was left alone rather than shipped
  unverified. Low priority: the webview loads only bundled assets and makes no
  network requests of its own (TMDB calls happen in Python).

- **Sidecar architecture.** Replace process-per-action with one long-lived
  backend speaking JSON-RPC over stdio, and push progress instead of polling.
  This is the biggest structural improvement available and unblocks
  low-latency UI, but it is a large change — do it once the tracks above are
  stable.
- **Optional transcode stage.** Raw MakeMKV output is 4-8 GB/hr (DVD) and
  20-40 GB (Blu-ray). An optional HandBrake/ffmpeg pass is the difference
  between a 8 TB and a 1 TB library. Needs a product decision first.
- **Batch/disc queue.** "Here are 40 discs of Show X season 3" as one unit of
  work, so a migration session is genuinely hands-off.
- **Split the monoliths.** `mapper.py` (~2,100 lines) mixes menu OCR, VLC
  screenshotting, DVD archaeology, and episode assignment. `App.tsx`
  (~2,500 lines) is one component with ~20 `useState` hooks.
- **Code signing.** Unsigned installers trigger SmartScreen. Deferred by
  decision; the README documents the click-through.

## Done

### 2026-08-23 — Tracks 1-3 (branch `feat/hardening-and-rip-improvements`)

- **Repo hygiene:** untracked `app/config.json` and broadened `.gitignore` so a
  TMDB key cannot leak; removed scaffolding leftovers; added
  `requirements.txt` / `requirements-dev.txt` with a pinned PyInstaller; added
  the root `README.md`, `CLAUDE.md`, and this file; CI now runs the tests and
  releases are gated on them.
- **Bundle and versioning:** deleted the legacy tkinter GUI and excluded Tcl/Tk
  (30 MB -> 21 MB); made the backend build resolve a Python that actually has
  PyInstaller (setup-python does not register with the `py` launcher); added
  `set-version.mjs` so the git tag is the single source of the version.
- **SQLite:** WAL + 30s busy timeout + schema DDL gated behind
  `PRAGMA user_version`, so the UI's 3s poll no longer opens a write
  transaction per tick.
- **Progress:** new `job_progress` table and `progress.py` module replace
  free-text log lines and regex parsing; `last_advance_at` drives stall
  detection.
- **Ripping:** title selection via the new `makemkv.py` (skips trailers,
  logos, playlist duplicates, and "play all" tracks), a configurable Blu-ray
  scan timeout, real MakeMKV robot progress parsing, and optional auto-eject.

Test suite grew from 9 to 31 cases.

### 2026-08-24 — bugs found by running it on real discs

- **`delete_job` was never registered.** `#[tauri::command(name = "...")]` is
  not a valid attribute and is silently ignored, so the command registered as
  `delete_job_cmd` while the frontend invoked `delete_job`. Added
  `check-commands.mjs`, which cross-checks every `invoke()` against
  `generate_handler![]` in CI -- neither tsc nor cargo can catch this.
- **Jobs aimed at an empty drive died opaquely.** `_ensure_drive_available`
  never checked for a disc. Now it does, and names the drive that has one;
  `_describe_makemkv_failure` translates MakeMKV's exit codes using its own
  `DRV:` lines.
- **Deleting a job failed on a foreign key.** `delete_job` used a hardcoded
  list of 8 child tables; 10 reference `jobs`. Now derived from
  `PRAGMA foreign_key_list`.
- **Resume did nothing on errored jobs.** `run_pipeline_for_job` had no branch
  for `error`, and `error -> queued` would have re-ripped the disc. Errored
  jobs now resume at the stage their artifacts justify.

Test suite grew from 31 to 55 cases.

### 2026-08-24 — transfer speed and failure clarity

- **Checksums are computed while copying**, not by reading the finished file
  back off the NAS. The old read-back also compared nothing -- the source was
  never hashed -- so it was pure cost that could not detect corruption.
  Roughly halves transfer time on a network share.
- **`verify_transfers`** (default off) turns that read-back into a real
  comparison against the streaming hash, so the expensive pass now buys
  genuine end-to-end verification for anyone who wants it.
- **`ensure_nas_available`** fails the copy stage immediately with an
  actionable message, and logs a non-fatal warning before a rip starts, rather
  than surfacing a raw WinError from a mkdir after the rip and rename are done.

Test suite grew from 55 to 65 cases.

### 2026-08-24 — runtime disambiguation

`_score_runtime_hint` bucketed the ripped duration into a coarse prior without
ever looking at the candidate, so same-title films were indistinguishable and
fell through to manual review. The leading candidates now get a real runtime
from the detail endpoint (cached) and are scored on the difference.

Measured against ten labels from the job history that had required manual
review: correct auto-selections went from 2/10 to 5/10, with no wrong
auto-selections. Every remaining block is genuine ambiguity -- Overboard 1987
and 2018 both run exactly 112 minutes; Robin Hood 1973 (83m) and 1991 (86m)
are within PAL-speedup distance of each other; and for SINBAD the correct film
is not in TMDB's results at all, which is precisely the case where a confident
pick would have written a mis-named file to the NAS.

That last case needed a new guard: when several candidates share the leader's
title, runtime only confers confidence if the match is near-exact *and* clearly
better than every rival. Test suite grew from 65 to 91 cases.

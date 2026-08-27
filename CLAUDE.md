# CLAUDE.md

Orientation for Claude Code (and human contributors) working in this repo.
Read this before making changes. For current work-in-progress and what to do
next, see [`docs/PROGRESS.md`](docs/PROGRESS.md).

## What this is

Auto-Ripper automates ripping DVDs/Blu-rays into a Plex-shaped library. A
Tauri 2 + React desktop shell drives a Python backend that orchestrates
MakeMKV, ffmpeg, and TMDB. Windows-only today.

## Architecture

```
frontend/src/App.tsx   React UI, polls the backend for job state
        |
        v  invoke("command_name", args)
frontend/src-tauri/src/main.rs   Tauri commands; spawns the Python CLI
        |
        v  autorippr-backend.exe <subcommand>  (or `py -3.11 app/main.py` in dev)
app/main.py            argparse CLI -- the entire backend API surface
        |
        v
app/autorippr/*.py     pipeline stages
        |
        v
SQLite (staging_root/autorippr.db)   all job state; also the IPC channel
```

**The Rust layer holds no state.** Every UI action shells out to a fresh Python
process; `pipeline run` is spawned detached and the UI discovers what happened
by polling `job snapshot` (a full JSON dump of one job) every 3 seconds. SQLite
is therefore both the database and the message bus between processes.

Adding a backend capability means: add a CLI subcommand in `app/main.py` → add
a `#[tauri::command]` in `main.rs` that shells to it → add a wrapper in
`frontend/src/api.ts` → call it from `App.tsx`. All four layers, every time.

### Module map (`app/autorippr/`)

| File | Responsibility |
| --- | --- |
| `config.py` | Loads/validates `config.json`, applies env overrides |
| `db.py` | Schema, migrations, connection setup |
| `state.py` | Job state machine and transition validation |
| `pipeline.py` | Stage orchestration; the top-level `run_pipeline_for_job` |
| `rip.py` | Drive discovery, MakeMKV invocation, ffprobe, eject |
| `makemkv.py` | Robot-output parsing and title-selection heuristics (pure, unit-tested) |
| `progress.py` | The `job_progress` table: one upserted row per job |
| `tmdb.py` | TMDB search, candidate scoring, selection |
| `mapper.py` | Title→episode mapping, DVD menu analysis, OCR (largest file) |
| `splitter.py` | Splitting combined episodes on chapter boundaries |
| `naming.py` | Plex naming and local finalization |
| `transfer.py` | NAS copy with retries and checksums |
| `job_ops.py` | Delete/cancel/clear/remap operations |

### Job state machine

Defined in `state.py` and strictly enforced — `transition_job` raises
`InvalidTransitionError` on an illegal move.

```
movie:  queued -> ripping -> identifying -> renaming -> copying -> done
tv:     queued -> identifying -> ripping -> mapping -> splitting -> renaming -> copying -> done
                                                    \-> renaming (nothing needs splitting)
any active state -> error -> <the stage it failed in> (retry)
```

The two media types leave `queued` in opposite directions, and both edges must
exist in `ALLOWED_TRANSITIONS`. They did not: `queued -> identifying`,
`identifying -> ripping` and `ripping -> mapping` were all absent, so every TV
job died on its first step from the first commit until 2026-08-27. Nothing
tested the TV path, so nothing said so.

TV and movie jobs take different paths: **TV identifies before ripping**
(so mapping knows the episode list), **movies rip first**.

## Conventions

- **Python is standard-library only.** Do not add pip dependencies without a
  strong reason — it keeps the PyInstaller bundle small and the supply chain
  narrow. External capability comes from detected system tools, not packages.
- **Never bundle MakeMKV or ffmpeg.** They are detected prerequisites with
  their own licenses. See `specs/github-release-packaging-spec.md`.
- **Every CLI subcommand that the UI calls must print JSON to stdout** and
  nothing else, because Rust parses it with `serde_json`.
- **Long-running work must stay cancellable.** Loops that run for minutes poll
  the job's status in the DB and abort if it left the expected state — see
  `_job_is_still_ripping` in `rip.py`.
- Progress goes in the `job_progress` table, not into log messages.
- **`--minlength` must match between the disc scan and the rip.** MakeMKV
  renumbers titles after length filtering, so scanning with one value and
  ripping with another makes selected title IDs point at different titles.
- Boolean config keys need coercion in `App.tsx` (`BOOLEAN_CONFIG_KEYS`)
  because the settings form stores every field as a string.
- Config keys are added in three places: `config.py` (`AppConfig` +
  validation), `app/config.example.json`, and the Settings tab in `App.tsx`.

## Working on this repo

Run the app in dev mode (backend edits take effect immediately — no rebuild):

```bash
cd frontend && npm run tauri -- dev
```

Run backend tests:

```bash
py -3.11 -m unittest discover -s app/tests -t app/tests
```

Run frontend tests (the pure helpers in `src/lib.ts`):

```bash
cd frontend && npm test
```

Exercise the pipeline without a disc (creates fake MKVs):

```bash
py -3.11 app/main.py --config app/config.json pipeline run <job-id> --mock-rip
```

Build a release installer:

```bash
cd frontend && npm run build:self-contained && npm run tauri -- build
```

### Gotchas

- **Dev vs. bundled backend:** `resolve_runtime_paths()` in `main.rs` prefers
  `app/main.py` in debug builds and the PyInstaller exe in release builds. So a
  backend fix is live in `tauri dev` immediately while the bundled exe still
  carries the bug -- the dev app looks correct and the installer ships broken.
  `beforeBuildCommand` therefore runs `build:self-contained`, so a release
  build always regenerates the backend and cannot package a stale one. Do not
  weaken that back to `npm run build`.
- **User config is not repo config.** The app reads
  `%APPDATA%\Auto-Ripper\config.json`, seeded from `config.example.json`.
  Editing `app/config.json` only affects direct CLI runs.
- **Never commit a real TMDB key.** `app/config.json` is gitignored;
  `config.example.json` is the only config in version control.
- **`tkinter` is deliberately excluded** from the PyInstaller bundle. Do not
  import it in backend code — the legacy Tk GUI was removed because it pulled
  ~20 MB of Tcl/Tk into every release.
- **Do not keep the checkout in a cloud-synced folder.** This repo used to live
  under OneDrive, and its cloud-files engine follows `target/` and holds files
  open mid-build: `tauri build` compiled cleanly and then failed with
  `failed to bundle project: Access is denied (os error 5)` on a file no user
  process held -- the Windows Restart Manager reported nothing, because it is a
  filter driver. It was also the source of git's "dubious ownership" error.
  Both went away on moving the checkout out of the sync root.
  `run-tauri.mjs` still detects a synced checkout and redirects
  `CARGO_TARGET_DIR` outside it, so building from one works, but keeping the
  repo out of sync in the first place is better -- it also stops gigabytes of
  `node_modules` and `target` being uploaded.
- Always build via `npm run tauri -- build`, not `npx tauri build`: the wrapper
  supplies the local Tauri CLI when none is installed globally, and applies the
  sync-folder redirect.

## Testing

Backend tests are stdlib `unittest` under `app/tests/`, no framework. Frontend
tests are vitest over `src/lib.ts`, which holds the pure presentation helpers
extracted out of `App.tsx` — anything testable without a browser belongs there
rather than inside the component.

Both run in CI on every push. When fixing a bug, add a case that fails without
the fix, and *check that it does*: several tests here were written after a bug
reached a real disc, and one early attempt at a regression turned out to be
mathematically equivalent to the original code.

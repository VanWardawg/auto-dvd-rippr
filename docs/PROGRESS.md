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

- **Show rip throughput, and warn when it is pathological.** A disc reading at
  1x logs no errors and never stalls, so every current health check passes
  while a 75-minute film takes 84 minutes. Measured 4.8x vs 1.05x on two
  drives of the same machine, same night, near-identical discs.

- **Strip disc-junk tokens from labels.** `PRINCESS_BRIDE_CE`, `EVERAFTER169`
  (16:9), `PAW_PATROL_NA`, `THESECRETLIFEOFWALTERMITTY` (no spaces).
- **Batch review queue** so several discs can be cleared in one sitting.
- **Cap menu analysis runtime.** Median is 1.1 min but one job burned 11 min
  for nothing. A 3 min ceiling costs nothing.

- **Clear the progress row when artifacts are cleared.**
  `clear_job_local_artifacts` and `clear_job_output_artifacts` leave the
  `job_progress` row behind, so the UI can briefly show stale rip progress.
  Cosmetic.

### 2026-08-27 — parallel rips, and three bugs found underneath them

Ran two rips at once deliberately, to settle whether MakeMKV contends across
drives. It does not. E: ripped an 81-minute feature in 13.6 min at 4.8x while
F: ripped throughout — squarely inside the 11–18 min solo baseline measured
over 152 completed movie jobs. F: was slower, but got *slower still* after E:
finished, which rules out contention from both directions.

Worth knowing for future measurements: none of the 157 completed rips in the
database had ever overlapped, so there was no historical baseline to compare
against. Every earlier parallel attempt had been cancelled or deleted.

Three real bugs surfaced while watching.

**A crashed pipeline left a zombie job.** F:'s backend process died six
seconds after E: began its finalize and NAS-copy burst. `sqlite3.Operational-
Error` is not one of the domain exceptions the pipeline catches, so it escaped
both handlers and killed the process with the job still marked `ripping`.
MakeMKV survived its parent and kept ripping with nobody reading it; the job
was not resumable, because Resume only applies to errored jobs; and stderr went
to `Stdio::null()`, so the traceback was lost. All three are fixed: unexpected
exceptions mark the job errored, the detached backend logs to
`backend-errors.log`, and recording the error is best-effort so a broken
database cannot mask the original exception.

**A dead writer read as perfectly healthy.** The stall check compared the
progress row against itself — `updated_at` against `last_advance_at` — and both
freeze at the same instant when the process writing them dies. The difference
stays zero forever. There is now a wall-clock check, and an abandoned rip says
something different from a stalled one, because the remedies differ.

**MakeMKV exits 0 when it saves nothing.** A damaged disc logged "Encountered
11 errors of type 'Read Error'", "0 titles saved, 1 failed", and `EXIT_CODE: 0`.
The caller saw success, found no files, and reported "rip completed but no MKV
files were found". This had silently disabled the alternate-title retry added
the day before, which keys off a failed rip and so could never have fired on
the discs it exists for. A rip that produces no new MKV is now a failure
whatever the exit code says.

The failure description was no better: its unreadable-sector branch required
the words "hash check", so a log with eleven documented read errors fell
through to "failed with non-zero exit code" — useless, and untrue.

**Throughput is a better health signal than error count.** F:'s disc logged
zero errors and never stalled, yet read at 1–2x against E:'s 4.8x on the same
machine — the signature of silent hardware retries on marginal media. Nothing
in the app can currently see that. Worth surfacing MB/s in the progress row
and warning below roughly 2x sustained.

### 2026-08-26 — worn discs

A PONYO DVD failed to rip: `MEDIUM ERROR:L-EC UNCORRECTABLE`, 36 read errors,
17 of them at one offset in `VTS_12_1.VOB`. Ripping every title on the disc
worked. The disc carried the feature twice -- titles 0 and 1, both 102.5
minutes, 444 KB apart in size -- and selection collapsed them to the larger
one, which was the copy on the scratched sectors.

Size is a fine tiebreak between identical-length titles (Blu-ray playlist
obfuscation makes the largest the complete version rather than a partial
angle), but it says nothing about whether the disc is readable there. The
copies sit on different physical sectors, so the loser of that tiebreak is
now kept as a fallback and retried when the chosen title fails. Partial
output from the failed attempt is deleted first, or the pipeline would count
the truncated file as a ripped title.

Only same-length titles qualify as fallbacks: retrying a 102-minute feature
with a 3-minute trailer would produce something that looks like a successful
rip and is not the movie.

Drive cards also persist now. They were React state only, so every reload --
and every HMR update while editing the frontend -- dropped back to one blank
card, and a two-drive machine had to be re-set-up by hand each session.

### 2026-08-26 — pre-public audit

Scanned the history before making it public: no secrets, no private IPs, no
NAS hostnames. The one string that looked exactly like a TMDB key was cargo's
CACHEDIR.TAG signature, which is the same constant everywhere.

- `mapper.py` had an absolute path with a username baked in, which was both a
  privacy leak and a branch that could only ever match on one machine.
- `naming.py` (390 lines) and `splitter.py` (270 lines) had **no tests at all**,
  despite deciding what every file is called and where episodes are cut. Both
  now covered: 41 cases, verified by injecting real regressions and confirming
  they fail.
- Added issue templates, SECURITY.md and CONTRIBUTING.md.

Still uncovered: `mapper.py` (2,107 lines, the OCR and episode-assignment
logic) and the frontend. `dvdnav_menu.py` swallows 10 exceptions silently,
which makes a genuine failure indistinguishable from "optional dependency not
installed".

## Before the first public release

- [ ] **Make the repository public.** It is private today, so nobody can
      download a release from it. This is the single blocking item.
- [ ] **Tag `v0.1.0`** and let the Release workflow build it. The workflow
      stamps the version from the tag, gates on tests and the command-wiring
      check, rebuilds the backend, generates SHA256SUMS, and publishes a
      **draft** release so the notes can be checked first.
- [ ] **Rip one disc end to end on the packaged build.** The installers now
      carry the right backend, but no disc has yet gone through the PyInstaller
      path, which is where a missing hidden import would surface.
- [ ] Optional: code signing. Deferred; the release notes tell users about the
      SmartScreen prompt and point at the checksums.

## Not started

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

### 2026-08-24 — hands-off disc swapping

Targets the 107 hours of waiting rather than the compute. The drive is now
released the moment nothing needs the disc, which for a movie is after
identification resolves -- **including when the job pauses for review**, which
is exactly when the drive would otherwise be held for 35 minutes or overnight.

- `eject_after_rip` used to fire at the end of the rip, which was both too
  early (DVD menu analysis reads VIDEO_TS off the drive, so enabling it
  silently disabled a fallback that succeeds 17% of the time) and too late to
  help (the job then held the drive through review anyway). Ejection moved
  into the pipeline, which knows when the disc is genuinely finished with.
- TV identifies before ripping, so its menu artifacts are cached right after
  the rip and the drive is released before mapping, which reads them from
  staging rather than the disc.
- Continuous mode no longer refuses to auto-start when a *finished* job shares
  the disc label. It matched jobs in any state, so re-inserting an
  already-ripped disc did nothing and reported "already has an active job" --
  a silent stall, which is the worst failure when nobody is at the screen.

Together: open tray, drop the next disc in, it starts. No UI interaction.
Test suite grew from 91 to 97 cases.

### 2026-08-25 — the packaged build, verified for the first time

Everything until now had only ever run in dev mode, which resolves the backend
to `app/main.py`. Release mode uses the PyInstaller executable instead, and
that path had never been exercised.

- `tauri build` was **broken on this machine**. It compiled, then failed at
  bundling with `failed to bundle project: Access is denied (os error 5)` on a
  file no user process held -- the Windows Restart Manager reported nothing,
  because the checkout is inside OneDrive and its cloud-files filter driver had
  the file open. `run-tauri.mjs` now redirects `CARGO_TARGET_DIR` outside the
  sync root when it detects one; CI and non-synced checkouts are unaffected.
  The checkout was subsequently moved out of OneDrive, which fixed this and the
  git "dubious ownership" error at the source; a release build from the new
  location produces both bundles at the standard in-tree path that CI globs.
- With that fixed, both bundles build: `.msi` and an NSIS `-setup.exe`.
- The packaged app was launched and verified: window opens, config loads, all
  174 jobs list (so the bundled backend is genuinely being invoked), staging
  capacity reports, and the optical drive is detected.
- The bundled backend was also run directly, and migrated the live database to
  schema 3 correctly.
- **Content Security Policy is now enabled and verified** against that
  packaged build, which is what it was waiting on. The app renders and IPC
  works under `default-src 'self'`; `style-src` allows inline because React
  writes the progress-bar widths that way.

### 2026-08-24 — UI pass

The interface had accumulated by addition: "Settings" appeared three times on
screen, the job list sat below two other cards, and a finished job announced
itself as complete in four separate places.

- `styles.css` rebuilt on design tokens, with a light and a dark palette and a
  theme toggle that persists. Every colour resolves from a token, so adding a
  theme means adding a palette, not editing rules. Contrast was measured
  rather than eyeballed: all text/background pairs now clear WCAG AA.
- Sidebar reordered to header -> start disc -> jobs, with the job list
  flexing to fill. The redundant "App" utility card is gone; its buttons moved
  to where they belong (Settings in the header, Reload/Auto-detect inside
  Settings, Refresh on the jobs list).
- Job header shows the title, subtitle, and one metadata line instead of four
  restatements of the same status.
- Stage pills became a real stepper with connectors; the progress bar carries
  a state colour and animates only while genuinely working.
- Activity log is one dense row per entry rather than a card per entry.
- Overview tiles adapt to media type -- movies were showing "Scope:
  unspecified" and "Season: -" for half the row.
- "Clear Local Artifacts" was briefly demoted from the primary button on the
  mistaken reading that a destructive action had drifted into the primary slot.
  It is the intended next step: staging space is the binding constraint, and
  reclaiming it is what lets the next disc start. Restored, and rebuilt around
  the actual need -- see below.

### 2026-08-24 — staging space

- The clear action now names the amount it will free ("Free 5.5 GB") rather
  than describing the mechanism.
- The sidebar carries a permanent staging capacity strip, turning red past 90%.
- `clear_local_after_transfer` (default off) deletes the staged rip once the
  NAS copy is confirmed.
- Bulk reclaim: one action frees every completed job's staging, named with the
  amount. Only finished jobs are touched -- an errored job may be one Resume
  away from finishing, and clearing it would turn a retry into a re-rip.
- A rip that cannot fit is refused before it starts, using the title sizes
  MakeMKV reports during the disc scan.
- The File menu (Settings / Reload Config / Quit) was removed: all three had
  duplicates in the app, and the menu bar cost a strip of vertical space.
- Settings is a full view rather than a panel stacked under the job progress,
  with a back arrow, a Done button, and Escape to close.
- The Windows title bar follows the app theme. It is drawn by the OS rather
  than the webview, so a dark UI kept a light title bar until the window was
  told separately via `set_theme`. It deletes files only and keeps the database records,
  so the NAS path and checksum survive; the manual action still clears rows
  too, which is what it has always done.

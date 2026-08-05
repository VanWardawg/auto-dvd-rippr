# DVD Auto-Ripper to NAS (Plex-Compatible) - Specification

## 1. Product goal

Build a **local Windows app** that:

1. Detects inserted DVDs.
2. Uses **MakeMKV** to rip content automatically.
3. Uses **TMDB API key** to identify show/movie and episode metadata.
4. Handles DVD quirks:
   1. combined episodes in one file (split required),
   2. episodes out of order,
   3. duplicate/alternate cuts.
5. Renames/files output in **exact Plex conventions**.
6. Copies finalized media to a NAS path reliably.

## 2. Scope

**In scope (MVP):**

- TV DVD workflow first (Bluey, Daniel Tiger, Paw Patrol class of content).
- Movie DVD basic support.
- Automated ripping from MakeMKV install.
- Metadata lookup via TMDB API.
- Split combined-episode files via FFmpeg.
- Plex-compliant naming/folder structure.
- Copy to NAS with retry + verification.
- Local UI for review/override before final copy.

**Out of scope (MVP):**

- Blu-ray advanced playlist obfuscation handling.
- Subtitle OCR/transcription.
- Multi-user web app / cloud-hosted service.

## 3. Hard requirements

### 3.1 Platform + runtime

- Windows 10/11 local app.
- Uses locally installed:
  - MakeMKV (`makemkvcon64.exe` preferred automation entrypoint from MakeMKV install),
  - `ffmpeg.exe` and `ffprobe.exe`.
- Works offline except for TMDB calls.

### 3.2 Config

- Config file (`config.json`) + env vars.
- Required config:
  - `tmdb_api_key` (or env `TMDB_API_KEY`)
  - `makemkv_path`
  - `ffmpeg_path`
  - `ffprobe_path`
  - `staging_root` (local temp/output)
  - `nas_root` (UNC path, e.g. `\\NAS\Media`)
  - `plex_library_type` (`tv`, `movies`, or both)
  - `default_order_mode` (`aired`, `dvd`, `absolute`)
  - `min_episode_minutes`, `max_episode_minutes` per show optional overrides

### 3.3 Plex output rules (must match Plex expectations)

**TV:**

- Folder: `Show Name (Year)\Season 01\`
- File (single episode):
  `Show Name (Year) - s01e01 - Episode Title.mkv`
- File (multi-episode):
  `Show Name (Year) - s01e01-e02 - Episode Title & Episode Title.mkv`
- Specials: `Season 00`, `s00eXX`

**Movies:**

- Folder: `Movie Name (Year)\`
- File: `Movie Name (Year).mkv`

No extra tags in filename (unless explicitly enabled).

## 4. Functional requirements

### FR-1 Disc ingest + rip

- Detect optical drive media insertion.
- Start rip job automatically (or prompt in UI if auto disabled).
- Rip all main titles to staging folder.
- Persist rip logs + title/chapter/duration metadata.

### FR-2 TMDB identification

- Parse disc label/volume name.
- Search TMDB TV/movie with fuzzy matching.
- Rank candidates by:
  - title similarity,
  - year proximity,
  - runtime similarity,
  - season hints from disc name.
- Require user confirmation when confidence below threshold.

### FR-3 Episode mapping engine

- Build expected episode list from TMDB for chosen season/order mode.
- Analyze ripped MKVs with ffprobe:
  - duration,
  - chapters,
  - streams.
- Map title-to-episode(s) with scoring.
- Detect:
  - combined episode files,
  - split episodes across multiple titles,
  - out-of-order episodes,
  - duplicates/alt cuts.
- Provide UI override grid: rip title ↔ episode assignment.

### FR-4 Combined-episode split

- If one MKV maps to multiple episodes:
  - split primarily on chapter boundaries,
  - fallback to manual timestamp entry.
- Generate separate episode files in Plex naming.
- Preserve quality (stream copy if possible, re-encode only when needed).
- Validate each split duration is within allowed range.

### FR-5 Ordering rules

- Support order modes per show/season:
  - aired (default),
  - dvd,
  - absolute.
- Allow manual episode sequence override and save profile for future discs.

### FR-6 Rename + finalize

- Rename/move from staging to Plex structure in local finalization folder.
- Detect collisions:
  - default = skip + flag in UI,
  - optional overwrite policy.

### FR-7 NAS transfer

- Copy finalized files to NAS path with:
  - retry/backoff,
  - partial-copy cleanup,
  - size/hash verification.
- Mark job complete only after verification.

### FR-8 Job management + recovery

- Persistent job state (SQLite).
- Resume interrupted jobs after restart.
- Statuses: `queued`, `ripping`, `identifying`, `mapping`, `splitting`, `renaming`, `copying`, `done`, `error`.
- Structured logs per job.

## 5. Non-functional requirements

- **Reliability:** resumable, idempotent operations.
- **Transparency:** every decision explainable in UI/log (“why mapped to S01E05”).
- **Performance:** process one disc end-to-end unattended; optional queue.
- **Security:** TMDB key never logged in plaintext.
- **Maintainability:** modular services; unit-testable mapping/splitting logic.

## 6. Suggested architecture (implementation target)

1. **Watcher Service** (drive detect, job creation)
2. **Rip Service** (MakeMKV wrapper + parsing)
3. **Metadata Service** (TMDB client + cache)
4. **Mapper Service** (heuristics + confidence scoring)
5. **Split Service** (ffmpeg/ffprobe orchestration)
6. **Naming Service** (Plex path/filename generation)
7. **Transfer Service** (NAS copy + verify)
8. **Local UI** (review queue, overrides, job logs)
9. **State Store** (SQLite + filesystem manifests)

## 7. Data model (minimum)

- `jobs`
- `rip_titles` (title id, duration, chapters, source file)
- `tmdb_candidates`
- `episode_mappings` (title → one/many episodes + confidence + manual_override)
- `split_plans` (source file, start/end/chapter boundaries)
- `outputs` (final path, checksum, transfer status)
- `show_profiles` (order mode + runtime heuristics per show)

## 8. Implementation breakdown for Sonnet/Haiku

1. **Foundation**
   - Config loader, SQLite schema, structured logging, job state machine.
2. **Ripping integration**
   - MakeMKV command runner + parse title metadata + error handling.
3. **TMDB integration**
   - Search/select APIs + candidate scoring + local cache.
4. **Mapper**
   - Heuristic matching + confidence + override support.
5. **Split pipeline**
   - Chapter/time splitting + validation.
6. **Plex naming/finalization**
   - Deterministic naming + conflict policy.
7. **NAS transfer**
   - Copy/retry/verify + resume.
8. **Local UI**
   - Job list, candidate selection, mapping editor, split editor, retry controls.
9. **Polish**
   - Show profiles for recurring kids’ discs; regression test fixtures.

## 9. Acceptance criteria

- Inserting a supported TV DVD can complete unattended when confidence is high.
- For combined episodes, app outputs separate correctly named Plex files.
- For out-of-order discs, final filenames reflect chosen order mode or manual override.
- Files appear on NAS in exact Plex folder/file format.
- Interrupted run resumes without re-ripping completed steps.
- Logs clearly explain mapping/splitting decisions and failures.

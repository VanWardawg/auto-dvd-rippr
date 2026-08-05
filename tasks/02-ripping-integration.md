# Task 02 - Ripping Integration (MakeMKV)

## Goal

Rip DVD titles into staging and capture title metadata for downstream mapping.

## Scope

- Discover optical drive/disc presence.
- MakeMKV invocation wrapper (prefer `makemkvcon64.exe`).
- Rip output into per-job staging folder.
- Parse and persist title metadata:
  - title ID
  - duration
  - chapter count
  - output file path
- Persist raw MakeMKV logs.

## Guidance

- Keep command invocation isolated in one service module.
- Track rip progress and update job status to `ripping`.
- If rip fails, set job to `error` with reason.
- Avoid assumptions about title ordering.

## Done when

- Inserting a test disc (or mock mode) creates ripped MKV(s) in staging.
- Metadata rows are saved for all ripped titles.
- Failures are visible in logs and state.

## Validation

1. Run rip job on known disc/mock -> at least one MKV created in job staging directory.
2. Verify `rip_titles` records exist for each MKV.
3. Inspect one saved log -> includes MakeMKV command + output.
4. Force a bad MakeMKV path -> job transitions to `error` with clear message.


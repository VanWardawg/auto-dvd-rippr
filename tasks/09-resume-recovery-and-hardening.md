# Task 09 - Resume, Recovery, and MVP Hardening

## Goal

Ensure the pipeline is dependable end-to-end under interruption and error conditions.

## Scope

- Resume logic for interrupted jobs across all stages.
- Idempotent stage handlers (safe re-run behavior).
- Structured error taxonomy and user-facing failure reasons.
- Baseline regression fixtures:
  - normal single-episode titles
  - combined episodes
  - out-of-order episodes
- End-to-end smoke workflow documentation (operator checklist).

## Guidance

- Persist checkpoints at stage boundaries and key sub-operations.
- On startup, detect incomplete jobs and enqueue resumable stage.
- Add regression tests around mapping/splitting/naming logic.

## Done when

- Mid-run app restart resumes jobs correctly.
- Re-running same stage does not duplicate/corrupt output.
- Core edge-case fixtures pass consistently.

## Validation

1. Interrupt during rip/split/copy, restart app -> job resumes from correct stage.
2. Re-run completed naming/copy stage intentionally -> no duplicate or conflicting output.
3. Run regression fixtures -> expected mapping/splitting/naming outputs match.
4. End-to-end trial disc completes with final files on NAS in Plex format.


# Task 01 - Foundation and State

## Goal

Create the project skeleton and core runtime plumbing so later tasks can be added without refactoring.

## Scope

- Config loading (`config.json` + env override support).
- Structured logging.
- SQLite schema for core entities.
- Job state machine with required statuses:
  `queued`, `ripping`, `identifying`, `mapping`, `splitting`, `renaming`, `copying`, `done`, `error`.
- Basic CLI/service entrypoint that can create a test job.

## Guidance

- Validate config at startup and fail with actionable messages.
- Store DB in app data folder; keep paths configurable.
- Make state transitions explicit (allowed transitions table/function).
- Log each state transition with job ID and timestamp.

## Done when

- App starts with valid config and rejects invalid config cleanly.
- Test job can be created, moved through sample states, and persisted.
- Restarting app keeps existing job records intact.

## Validation

1. Start app with missing `tmdb_api_key` -> clear startup error.
2. Start app with valid config -> app boots and creates DB schema.
3. Create test job -> verify row exists in DB with `queued`.
4. Advance through two states -> verify transitions and logs persisted.
5. Restart app -> verify same job/state still present.


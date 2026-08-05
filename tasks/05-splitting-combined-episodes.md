# Task 05 - Split Combined Episodes

## Goal

Split a single ripped MKV into separate episode files when mapping indicates combined episodes.

## Scope

- Split planning from chapter boundaries first.
- Manual timestamp override fallback.
- FFmpeg split execution.
- Stream copy when possible; re-encode only if required.
- Output validation (duration sanity checks).

## Guidance

- Save split plans before execution for auditability/retry.
- Keep original source MKV untouched.
- Emit deterministic output file names from mapping target episodes.
- Fail split step explicitly if segment generation fails.

## Done when

- Combined-episode source generates separate episode files.
- Split outputs pass duration sanity checks.
- Split metadata persisted for resume and debugging.

## Validation

1. Use a known combined-episode fixture -> produces two+ episode files.
2. Verify output durations are in expected range for target episodes.
3. Simulate bad chapter split -> apply manual timestamps and succeed.
4. Confirm split plan + execution results are persisted.


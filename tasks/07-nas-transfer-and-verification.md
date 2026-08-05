# Task 07 - NAS Transfer and Verification

## Goal

Copy finalized local outputs to NAS reliably and mark completion only after integrity checks.

## Scope

- NAS copy from finalization area to configured `nas_root`.
- Retry/backoff on transient failures.
- Partial-copy cleanup on failed attempts.
- Integrity verification (size and/or hash).
- Job state progression to `copying` and then `done`.

## Guidance

- Use atomic-ish behavior: copy temp then finalize target where feasible.
- Persist per-file transfer attempts and last error.
- Only mark file/job complete when verification passes.

## Done when

- Files are copied to NAS paths matching Plex layout.
- Transient errors can recover via retries.
- Verification failures keep job incomplete and visible.

## Validation

1. Copy outputs to reachable NAS path -> verify files exist and match expected size/hash.
2. Simulate temporary network failure -> verify retries happen and recovery succeeds.
3. Simulate permanent failure -> verify file/job remains failed with clear error.
4. Ensure no partially copied file is left as "successful".


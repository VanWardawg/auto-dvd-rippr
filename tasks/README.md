# MVP Task Pack - DVD Auto-Ripper

This folder breaks the spec into small, implementation-ready MVP tasks for Sonnet/Haiku.

## Execution order

1. `01-foundation-and-state.md`
2. `02-ripping-integration.md`
3. `03-tmdb-identification.md`
4. `04-episode-mapping.md`
5. `05-splitting-combined-episodes.md`
6. `06-plex-naming-and-finalize.md`
7. `07-nas-transfer-and-verification.md`
8. `08-review-ui-and-overrides.md`
9. `09-resume-recovery-and-hardening.md`

## Global implementation rules

- Keep all functionality local-first on Windows.
- Never log TMDB API keys or secrets.
- Persist state changes step-by-step so interrupted runs can resume.
- Fail loudly with clear errors; do not silently skip failed work.
- Keep modules isolated: watcher/rip/metadata/mapping/split/naming/transfer/ui/state.

## Global validation rule

Each task is complete only when its "Done when" and "Validation" sections both pass.


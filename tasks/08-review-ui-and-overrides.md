# Task 08 - Review UI and Overrides

## Goal

Provide a local UI that lets users review candidates/mappings/splits and override decisions before final copy.

## Scope

- Job list with status and progress.
- Candidate selection UI for TMDB result confirmation.
- Mapping override grid (title -> episode(s)).
- Split override editor (chapter or timestamps).
- Retry controls for failed steps.

## Guidance

- UI should show "why" for auto decisions (confidence + factors).
- Every override must be persisted and take precedence over auto logic.
- Keep UI focused on MVP workflows; avoid broad customization in this phase.

## Done when

- User can correct low-confidence TMDB/mapping decisions.
- User can set manual split points and continue pipeline.
- Retry from failed step works without restarting the whole job.

## Validation

1. Open a low-confidence job -> select different TMDB candidate -> pipeline uses override.
2. Override one title mapping -> output filenames reflect override target episodes.
3. Set manual split timestamps -> split succeeds using manual plan.
4. Retry an errored job from failed stage -> prior successful stages are not repeated unnecessarily.


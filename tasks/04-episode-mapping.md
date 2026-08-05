# Task 04 - Episode Mapping Engine

## Goal

Map ripped title files to expected episodes, including out-of-order content and multi-episode titles.

## Scope

- Pull expected episode list from TMDB by selected show/season/order mode.
- Analyze each ripped MKV with ffprobe (duration/chapter/stream metadata).
- Heuristic mapping from title -> one or many episodes.
- Detection flags:
  - combined episodes
  - out-of-order sequence
  - duplicate/alternate candidates
- Persist mapping confidence + explanation text.

## Guidance

- Keep mapping algorithm pure/testable (input model -> output model).
- Store both auto mapping and override slots.
- Treat low-confidence mappings as review-required.
- Do not assume one MKV equals one episode.

## Done when

- System generates an initial mapping plan for all ripped titles.
- Combined and out-of-order cases are detected and marked.
- Mapping results are persisted and explainable.

## Validation

1. Feed a fixture with one file containing two episodes -> mapping marks it multi-episode.
2. Feed a fixture with shuffled title order -> mapping still resolves to correct episode order.
3. Verify mapping records include confidence + reason.
4. Verify unresolved items are flagged for user review.


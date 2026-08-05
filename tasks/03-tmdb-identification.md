# Task 03 - TMDB Identification

## Goal

Identify the correct show/movie and season context using TMDB with confidence scoring.

## Scope

- TMDB client with API key auth.
- Disc label parsing + query normalization.
- Candidate search for TV and movies.
- Scoring model using:
  - title similarity
  - year proximity
  - runtime fit
  - season hints from disc label
- Confidence threshold and "needs review" flag when low confidence.

## Guidance

- Cache TMDB responses locally to reduce repeat calls.
- Keep scoring deterministic and explainable (store score breakdown).
- Never log API key.
- Record candidate list, chosen candidate, and reason.

## Done when

- App returns ranked TMDB candidates for a ripped job.
- High-confidence cases auto-select.
- Low-confidence cases are flagged for manual confirmation.

## Validation

1. Use a known show disc label (e.g., Bluey season disc) -> correct show appears in top candidates.
2. Verify score breakdown stored per candidate.
3. Simulate ambiguous label -> job marked for review, not auto-finalized.
4. Confirm logs/config dumps do not expose TMDB key.


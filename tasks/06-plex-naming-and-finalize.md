# Task 06 - Plex Naming and Finalization

## Goal

Move processed files into exact Plex-compatible folder/file naming structure.

## Scope

- Naming service for TV and movie outputs.
- Folder generation rules:
  - TV: `Show Name (Year)\Season 01\`
  - Movie: `Movie Name (Year)\`
- Filename rules:
  - TV single: `Show Name (Year) - s01e01 - Episode Title.mkv`
  - TV multi: `Show Name (Year) - s01e01-e02 - Episode Title & Episode Title.mkv`
  - Movie: `Movie Name (Year).mkv`
- Collision handling policy:
  - default skip + flag
  - optional overwrite mode

## Guidance

- Build naming from normalized metadata (no ad hoc string assembly in workflow code).
- Validate illegal path characters before move.
- Write finalization manifest for each output file.

## Done when

- Processed files land in exact Plex naming/path format.
- Collisions handled by configured policy.
- Manifest reflects final local paths.

## Validation

1. Run naming on sample TV set -> verify exact season folder + filename format.
2. Run naming on sample movie -> verify exact movie folder/file format.
3. Create deliberate filename collision -> verify skip/flag behavior.
4. Confirm finalization manifest lists all outputs and statuses.


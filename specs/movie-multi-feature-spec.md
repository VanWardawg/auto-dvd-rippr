# Movie Multi-Feature Support

## Goal

Support movie discs that contain more than one main movie while keeping the existing single-movie flow intact.

## Modes

Movie jobs gain a `movie_mode` field:

- `single`
- `double_feature`
- `trilogy`

`single` remains the default.

## Workflow

### Single movie

Keep the current flow:

1. rip
2. identify one TMDB movie
3. finalize one output
4. transfer

### Double feature / trilogy

Use a manual ordered selection flow:

1. rip
2. identify candidates
3. user manually selects movies in slot order:
   - slot 1
   - slot 2
   - slot 3 (for trilogy)
4. pipeline resumes once all required slots are selected
5. ripped main-title files are assigned to selected movies in disc/title order
6. finalize one output per selected movie
7. transfer each finalized output

## Backend model

### Jobs

Add `movie_mode` to `jobs`.

### Selected movies

Add a new table for multi-movie selections, one row per selected movie slot:

- `job_id`
- `slot_index`
- `tmdb_id`
- `title`
- `year`
- `rip_title_id`
- timestamps

For `single`, the existing `job_selected_media` row remains valid.
For `double_feature` and `trilogy`, the new slot table is the source of truth.

## Identification rules

- `single`: current auto/manual selection behavior
- `double_feature` / `trilogy`:
  - TMDB identify still generates candidates
  - pipeline does **not** auto-complete identify
  - review remains required until all movie slots are selected

## File assignment

Initial implementation uses:

- main ripped movie files in disc/title order
- selected movie slots in slot order

Then:

- slot 1 -> first main movie file
- slot 2 -> second main movie file
- slot 3 -> third main movie file

This is the default behavior for the first implementation.

## Finalization

For multi-movie jobs, finalization produces one output per selected movie:

- `finalized\<Movie Title (Year)>\<Movie Title (Year)>.mkv`

## Transfer

Transfer must work per output item and must not assume one selected movie per job.

## Frontend changes

### Start form

When `mediaType = movie`, show a `movieMode` field:

- Single
- Double Feature
- Trilogy

### TMDB review

For multi-movie jobs:

- show required slot count
- let the user choose a slot
- let the user assign a TMDB candidate to that slot
- show already-selected slot assignments

## Non-goals for first pass

- automatic multi-movie TMDB selection
- manual per-movie file reassignment UI
- heuristic movie-to-file matching beyond disc/title order

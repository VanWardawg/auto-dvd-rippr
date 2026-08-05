# Tauri + React Frontend Specification

## 1. Goal

Replace the current Tkinter GUI with a modern desktop frontend built with:

- **Tauri**
- **React**
- **TypeScript**

while preserving the current Python backend as the source of truth for:

- ripping
- TMDB identification
- DVD menu analysis
- mapping
- splitting
- finalization
- transfer

## 2. Architectural approach

### 2.1 Backend model

The Python backend remains the execution engine.

The frontend must **not** reimplement pipeline logic.
Instead it should call the Python CLI and consume JSON output.

### 2.2 Tauri role

Tauri provides:

- desktop shell
- native windowing
- command bridge between React and Rust
- process launching for Python commands
- filesystem-safe path handling
- artifact opening

### 2.3 React role

React provides:

- modern layout and styling
- drill-down screens
- tables/cards/status chips
- polling and optimistic UI state
- artifact browsing

## 3. Backend contract for frontend

### 3.1 Required Python commands

The frontend depends on these CLI commands:

- `job list`
- `job snapshot <job_id>`
- `job create ...`
- `job delete <job_id>`
- `job set-profile <job_id> ...`
- `pipeline run <job_id>`
- `mapping analyze-menu <job_id>`
- `mapping run <job_id>`
- `tmdb identify <job_id>`

### 3.2 Frontend command semantics

#### Synchronous reads

- `job list`
- `job snapshot`

These return JSON immediately and are used for polling/UI refresh.

#### Asynchronous mutations

- start job
- resume job
- analyze menu
- rerun mapping
- rerun identify
- delete job

These should be launched in background processes by Tauri and return quickly.

## 4. UI information architecture

## 4.1 Main layout

Three-column mental model, implemented responsively:

1. **Sidebar / job rail**
   - jobs list
   - status chips
   - search/filter later

2. **Main content**
   - current job overview
   - bundle mapping table
   - outputs table

3. **Inspector / artifact panel**
   - raw JSON
   - artifacts
   - logs
   - menu analysis details

## 4.2 Primary views

### Dashboard

- app header
- create/start card
- selected job summary
- quick actions

### Job overview

Show:

- status
- disc label
- scope / season / episode range
- selected TMDB media
- output count

### Bundle mapping table

For each mapped bundle show:

- source file
- episode range
- recovered titles
- confidence
- play-all indicator

### Outputs table

For each finalized output show:

- episode number
- final file name
- transfer status

### Artifact browser

Show structured artifacts:

- menu analysis
- bundle association
- VLC nav screenshots
- OCR artifacts
- dvdnav artifacts

Support:

- open externally
- copy path
- image preview (future/optional)

### Raw JSON view

Keep a debug-friendly view for power users.

## 5. User workflows

### 5.1 Start new job

Inputs:

- disc label
- media type
- disc scope
- season number
- episode range

Action:

- create job
- start pipeline in background

### 5.2 Analyze menu then map

1. select job
2. click **Analyze DVD Menu**
3. wait for cached artifacts
4. click **Re-run Mapping**
5. inspect bundle associations and confidence

### 5.3 Resume workflow

If job is paused:

- show current stage
- surface most relevant action
- allow resume

## 6. Visual design goals

The new frontend should look:

- modern
- airy
- summary-first
- production-like

### 6.1 Design choices

- dark text on light neutral background
- card-based layout
- sticky top action bar
- rounded panels
- status chips
- compact but readable tables
- restrained color coding

### 6.2 Explicitly avoid

- giant raw text dumps as primary UI
- all controls visible at once
- long button rows
- old desktop form-grid feel

## 7. Polling model

- jobs list poll: every 3 seconds
- selected job snapshot poll: every 4 seconds
- immediate refresh after actions

## 8. MVP implementation scope

### Included in first scaffold

- Tauri desktop shell
- React + TypeScript app
- jobs list
- selected job overview
- start job form
- quick action buttons
- outputs table
- bundle table
- artifacts list
- raw JSON tab
- Rust bridge to Python CLI

### Deferred

- inline image preview for artifacts
- advanced override dialogs
- drag/drop screenshot upload
- rich log timeline
- websocket/event-stream updates
- packaged Python distribution strategy

## 9. Rust/Tauri command surface

Commands to implement:

- `list_jobs`
- `job_snapshot(jobId)`
- `start_pipeline(request)`
- `resume_pipeline(jobId)`
- `analyze_menu(jobId)`
- `rerun_mapping(jobId)`
- `rerun_identify(jobId)`
- `delete_job(jobId)`
- `open_path(path)`

## 10. Packaging assumptions

Initial implementation assumes:

- local Python is available
- `app\main.py` exists in repo
- config remains at `app\config.json`

Packaged/distributable Python runtime is a later task.

## 11. Success criteria

The new frontend is successful if it:

1. looks substantially more modern than Tkinter
2. exposes the same core workflows
3. can start/resume/analyze/remap jobs through the Python backend
4. surfaces bundle association and artifacts clearly
5. keeps raw JSON available without making it the main interface

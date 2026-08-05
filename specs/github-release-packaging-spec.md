# Auto-Ripper GitHub Release Packaging Specification

## 1. Goal

Package Auto-Ripper as a Windows desktop app that can be distributed through **GitHub Releases** with:

- a user-friendly installer
- first-run setup and diagnostics
- external dependency detection
- clear guidance when required tools are missing
- a release process that is legal and maintainable

The app should remain free to use, and the project may accept optional donations. Distribution design must not depend on paid activation or a hosted service.

## 2. Distribution model

### 2.1 Primary release artifact

Publish a Windows installer built from the Tauri frontend.

Preferred release assets:

1. installer (`.msi` or Tauri Windows bundle)
2. portable zip (optional)
3. checksums / release notes

### 2.2 Supported platform

- Windows 10/11
- x64 first

## 3. Packaging principles

### 3.1 What may be packaged

May be packaged:

- Tauri desktop shell
- React frontend assets
- Rust bridge binary
- Python application code
- app configuration templates
- app icons / docs / onboarding content

### 3.2 What must not be bundled blindly

Must **not** be assumed bundle-safe without explicit legal review:

- **MakeMKV**
- any dependency with redistribution restrictions

### 3.3 External tool strategy

External tools must be treated as **detected prerequisites**:

- MakeMKV / `makemkvcon64.exe`
- `ffmpeg.exe`
- `ffprobe.exe`
- optional OCR / DVD analysis dependencies if those features require them

If a dependency is missing, the app must:

1. detect it
2. explain why it is needed
3. show install guidance
4. let the user retry detection
5. avoid crashing or requiring terminal use

## 4. First-run setup flow

## 4.1 Setup wizard

On first launch, show a guided setup screen before normal workflow.

Wizard steps:

1. welcome / overview
2. dependency checks
3. TMDB API key setup
4. staging location setup
5. NAS/library destination setup
6. validation
7. finish

## 4.2 Dependency checks

For each dependency, show:

- status: found / missing / invalid
- detected path
- why it is required
- install instructions
- browse button to set path manually
- recheck button

### Required dependency behavior

#### MakeMKV

Must detect:

- configured path
- sibling CLI fallback like `makemkvcon64.exe`
- common Windows install locations

If missing:

- explain that MakeMKV is required for DVD ripping
- link users to official install guidance
- explain that Auto-Ripper cannot ship it directly

#### FFmpeg / FFprobe

Must detect:

- configured path
- common install locations
- PATH-based discovery if available

If missing:

- explain that FFmpeg is required for inspection/splitting
- offer path picker

## 4.3 Config persistence

The setup wizard should write config through the app UI, not require manual JSON editing.

Config should persist in a user-writable location.

Preferred behavior:

- keep shipped defaults separate from user config
- allow resetting or editing config later from Settings

## 5. Application requirements for release readiness

## 5.1 Settings UI

Add or complete a persistent Settings screen for:

- TMDB API key
- MakeMKV path
- FFmpeg path
- FFprobe path
- staging root
- NAS root
- default media mode options
- continuous mode defaults (if enabled)
- optional feature toggles

## 5.2 Health / diagnostics screen

Add a diagnostics view that shows:

- app version
- dependency detection status
- current config summary
- database path
- staging path
- NAS availability
- recent backend errors
- buttons to open logs / staging folders

## 5.3 Error handling

For packaged releases:

- fatal startup issues must show user-friendly dialogs/screens
- backend exceptions must surface in UI in plain language
- dependency/path errors must tell users what to do next

## 5.4 Logging

Keep logs in a stable user path and expose them from the UI.

Must include:

- app version
- command invocation
- dependency detection results
- rip / identify / finalize / transfer errors

## 6. Python runtime packaging

The release must not assume users can run Python manually.

Preferred approaches:

1. package Python runtime with the app
2. or package the backend into a self-contained executable

Goal:

- end users launch Auto-Ripper from the installer
- no separate Python install required

## 6.1 Backend invocation contract

Tauri currently shells out to the Python backend.

For release packaging, update the bridge so it can locate the packaged backend reliably in both:

- development mode
- installed release mode

This must not rely on the repo source tree existing after install.

## 7. Installer / onboarding UX

## 7.1 First usable experience

A fresh user should be able to:

1. install the app
2. launch it
3. complete setup wizard
4. fix any missing dependency paths
5. detect a disc
6. start a job

without editing files or using a terminal

## 7.2 Update behavior

Initial release may use manual GitHub Releases downloads.

Later optional enhancements:

- in-app update check
- release notes dialog

## 8. Legal / compliance notes

## 8.1 MakeMKV

The spec must explicitly document:

- Auto-Ripper depends on user-installed MakeMKV
- Auto-Ripper does not redistribute MakeMKV in release assets unless redistribution terms are confirmed

## 8.2 FFmpeg

If FFmpeg is bundled in the future, document license obligations clearly.

Until then, prefer detection + install guidance unless the project is ready to manage redistribution requirements.

## 8.3 Donations

Optional donations are compatible with this release model.

If donations are accepted:

- keep the app functional without payment
- avoid implying bundled third-party tools are sold by this project
- add a clear Donations / Support link in About or Settings

## 9. GitHub release contents

Each release should include:

- installer asset
- versioned changelog / release notes
- supported OS note
- dependency requirements
- quick-start instructions
- known limitations

## 10. Required implementation work

## 10.1 Backend / runtime

- decouple packaged runtime from repo-root assumptions
- support installed-path backend lookup
- ensure config path is user-safe in installed mode
- ensure staging/log/db default paths are valid outside the repo

## 10.2 Frontend

- setup wizard
- settings screen
- dependency health screen
- first-run validation gate
- friendly missing-tool guidance

## 10.3 Tauri packaging

- Windows app metadata
- icons / product naming
- installer configuration
- release build validation

## 10.4 Documentation

- install guide
- dependency setup guide
- FAQ / troubleshooting
- release checklist

## 11. Nice-to-have release features

- one-click “Open logs”
- one-click “Open staging folder”
- environment export for bug reports
- clear “copy diagnostic summary” button
- safe config backup / restore

## 12. Non-goals for first packaged release

- bundling MakeMKV
- cloud sync / hosted account system
- mandatory donations or license gates
- macOS / Linux packaging

## 13. Recommended rollout plan

### Phase 1

- stabilize Tauri UX
- add settings + dependency checks
- make installed runtime work without repo tree

### Phase 2

- add setup wizard
- add diagnostics screen
- validate clean-machine install flow

### Phase 3

- produce signed or unsigned GitHub release builds
- publish install docs
- test user onboarding end to end

## 14. Acceptance criteria

Auto-Ripper is ready for GitHub Releases when:

1. a clean Windows machine can install and launch the app
2. the app can detect missing dependencies without crashing
3. users can configure required paths entirely in the UI
4. the packaged backend runs without the source repo present
5. logs and diagnostics are accessible in-app
6. the release notes clearly explain MakeMKV/FFmpeg prerequisites

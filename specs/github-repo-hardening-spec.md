# Auto-Ripper GitHub Repository Hardening Specification

## 1. Goal

Make the Auto-Ripper repository safe to:

- push to GitHub
- collaborate on publicly
- build in CI
- publish from GitHub Releases

without leaking secrets or shipping machine-specific configuration.

This spec is specifically about **repository hygiene, secret handling, CI, enforcement, and release discipline**.

## 2. Current risk to address

The repository must not contain live secrets or machine-specific config values.

Examples of unsafe content that must not remain committed:

- real TMDB API keys
- personal NAS paths
- local drive letters and machine-specific executable paths as committed defaults
- staging DB/log/output files
- user-generated artifacts

## 3. Secret handling

## 3.1 TMDB API key policy

The real TMDB API key must **not** live in committed source-controlled config.

Required changes:

1. Remove the live key from `app\config.json`
2. Replace it with:
   - empty string, or
   - obvious placeholder like `YOUR_TMDB_API_KEY`
3. Add a committed template such as:
   - `app\config.example.json`
4. Ensure runtime config is loaded from:
   - user config file, or
   - environment variable, or
   - setup wizard entry

## 3.2 Environment variables

Support secrets through environment variables for development and CI:

- `TMDB_API_KEY`

If env and config both exist, define a clear precedence rule and document it.

Recommended:

1. environment variable
2. user config file
3. shipped example/default config

## 3.3 GitHub secrets

For CI or release workflows, secrets must be stored in:

- GitHub Actions Secrets
- or GitHub Environments with protected secrets

Do not store production or developer secrets in:

- repo files
- workflow YAML
- committed `.env`

## 4. Config strategy for a public repo

## 4.1 Committed files

Recommended committed files:

- `app\config.example.json`
- optional `app\config.defaults.json`

These may include:

- placeholder paths
- safe defaults
- non-secret settings

They must not include:

- real API keys
- machine-specific absolute paths that imply required install layout

## 4.2 Ignored files

The real user config file should be ignored if it contains user secrets or machine-specific values.

If `app\config.json` remains the runtime file in development:

- it should either be:
  - generated from template, or
  - `.gitignore`d and replaced by an example file in source control

## 5. Required .gitignore coverage

The repo should ignore at minimum:

- Python caches:
  - `__pycache__/`
  - `*.pyc`
- Node artifacts:
  - `node_modules/`
  - frontend build output
- Tauri build output:
  - `frontend\src-tauri\target/`
  - release bundles if generated locally
- local config and env:
  - `.env`
  - `.env.*`
  - user runtime config if it contains secrets
- local staging/output:
  - staging DB
  - logs
  - ripped files
  - finalized outputs
  - OCR/menu artifacts
- editor/system noise:
  - `.DS_Store`
  - `Thumbs.db`
  - `.vscode` settings that are user-specific if needed

## 6. Pre-push / pre-commit protections

## 6.1 Secret scanning

Add a repo-level protection against accidental commits of:

- API keys
- tokens
- credentials
- private URLs

Recommended options:

1. GitHub secret scanning / push protection if available
2. local pre-commit hook for secret detection
3. CI secret scan as a fallback

## 6.2 Suggested local hook checks

Before allowing commits or pushes:

- reject committed `config.json` if it contains a non-placeholder TMDB key
- reject obvious secret patterns
- reject staging DB/log/output files
- reject large binary rip artifacts

## 7. CI / GitHub Actions requirements

No `.github\workflows` directory currently exists. Add it.

Recommended workflows:

## 7.1 Validate and build

Trigger:

- push
- pull_request

Checks:

1. frontend install
2. frontend type/build check:
   - `npm run build`
3. Python syntax/import smoke checks
4. optional packaging smoke build for Tauri on Windows runners

## 7.2 Secret / config guard

Trigger:

- push
- pull_request

Checks:

1. fail if committed config contains a real TMDB key
2. fail if ignored runtime artifacts appear in git
3. fail if forbidden files are present:
   - staging DB/logs
   - rip outputs
   - finalized media

## 7.3 Release workflow

Trigger:

- tag push
- manual workflow dispatch

Tasks:

1. build release artifact
2. attach installer/zip to GitHub Release
3. publish checksums
4. include release notes

## 8. Tests and validation needed before public push

## 8.1 Repo hygiene tests

Before first public push:

1. verify no live secrets remain in git-tracked files
2. verify no staging DB/log/media artifacts are tracked
3. verify config template works for a fresh developer

## 8.2 Functional validation

At minimum, CI or manual checks should confirm:

1. frontend builds cleanly
2. Tauri command bridge compiles
3. Python CLI launches with template/default config
4. missing dependency detection produces user-facing errors rather than crashes

## 8.3 Regression checks

Critical areas to keep guarded:

- movie identify
- TMDB review resume behavior
- local cleanup / rebuild output actions
- continuous mode duplicate-start protection
- transfer path sanitization

## 9. Release enforcement rules

Before publishing a GitHub release:

1. no secrets in tracked files
2. release workflow passes
3. Windows install/build passes
4. config template and onboarding docs are up to date
5. release notes mention external prerequisites:
   - MakeMKV
   - FFmpeg / FFprobe

## 10. Documentation requirements

Add or update docs for:

- how to set up local development safely
- how to create a real runtime config from the example template
- how to provide TMDB API key without committing it
- how to install prerequisites
- what GitHub Actions checks are expected
- how maintainers create a release

## 11. Suggested implementation tasks

### Repo hygiene

- add/update `.gitignore`
- add `config.example.json`
- remove live key from tracked config
- decide whether real `config.json` should remain tracked

### CI

- create `.github\workflows\validate.yml`
- create `.github\workflows\release.yml`
- add config/secret guard checks

### Dev ergonomics

- optional pre-commit hook support
- optional script to bootstrap local config from template

## 12. Acceptance criteria

The repo is GitHub-ready when:

1. no live secret exists in tracked files
2. a fresh clone can build with documented setup
3. CI enforces frontend build and repo hygiene
4. release workflows can build publishable artifacts
5. contributors can understand config and dependency setup without private knowledge

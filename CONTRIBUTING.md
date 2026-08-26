# Contributing

Bug reports with logs attached are the most useful thing you can send. If you
want to change code, this is how the project works.

## Getting set up

You need Node 20+, Rust (stable), Python 3.11, and the external tools listed in
the README.

```bash
py -3.11 -m pip install -r requirements-dev.txt
```

```bash
cd frontend && npm ci && npm run tauri -- dev
```

Backend edits take effect immediately in dev mode; the frontend hot-reloads.

## Before opening a pull request

```bash
py -3.11 -m unittest discover -s app/tests -t app/tests
```

```bash
cd frontend && npx tsc --noEmit && npm run check:commands
```

CI runs all three on every push, plus a full installer build.

## Conventions worth knowing

[`CLAUDE.md`](CLAUDE.md) is the orientation document — architecture, the job
state machine, and the gotchas that have actually caused bugs here. Read it
before a first change. The ones that bite most often:

- **The Python backend is standard-library only.** External capability comes
  from detected system tools, not pip packages. This keeps the PyInstaller
  bundle small and the supply chain narrow.
- **Adding a backend capability touches four layers**: a CLI subcommand in
  `app/main.py`, a `#[tauri::command]` in `main.rs`, a wrapper in `api.ts`, and
  the call in `App.tsx`. `npm run check:commands` verifies the middle two agree.
- **Commands the UI calls must print JSON and nothing else**, with types that
  match the Rust structs. SQLite has no boolean type, so anything Rust declares
  as `bool` needs coercing on the way out — see `test_cli_json_contract.py`.
- **Never bundle MakeMKV or ffmpeg.** They are detected prerequisites with
  their own licences.
- **Do not check out into a cloud-synced folder.** OneDrive and friends hold
  build output open and break bundling in a way that is hard to diagnose.

## Tests

Tests are stdlib `unittest`, no framework. When fixing a bug, add a case that
fails without the fix — and check that it does, rather than assuming. Several
tests here were written after a bug reached a real disc.

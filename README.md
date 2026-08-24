# Auto-Ripper

Automated DVD and Blu-ray ripping for a Plex-style media library.

Put a disc in the drive and Auto-Ripper identifies it against TMDB, rips only
the titles you actually want, maps them to episodes, splits combined episodes,
renames everything to Plex naming conventions, and copies the result to your
NAS — resuming cleanly if anything is interrupted.

> **Status:** active development, Windows-only for now. Expect rough edges.

---

## What it does

| Stage | What happens |
| --- | --- |
| **Identify** | Reads the disc label and menu, searches TMDB, and scores candidates. Falls back to DVD-menu OCR when the label is uninformative. |
| **Rip** | Scans the disc with MakeMKV, selects the titles worth keeping, and rips them — skipping trailers, logos, and duplicate angles. |
| **Map** | Matches ripped titles to TMDB episodes using duration, chapter counts, and menu text. |
| **Split** | Detects "two episodes in one title" discs and splits them on chapter boundaries with ffmpeg. |
| **Rename** | Applies Plex naming (`Show (Year)/Season 01/Show - s01e02 - Title.mkv`). |
| **Transfer** | Copies to your NAS with retries, checksum verification, and `.part` staging. |

Every stage is resumable, and anything the automation is unsure about is
surfaced in the UI for a one-click override rather than being guessed at.

## Requirements

Auto-Ripper is a coordinator — it drives external tools rather than bundling
them. You need these installed before it can rip anything:

| Tool | Required | Purpose |
| --- | --- | --- |
| [MakeMKV](https://www.makemkv.com/) | **Yes** | Reads and decrypts discs. The free beta key works but expires periodically; Auto-Ripper warns you before it does. |
| [ffmpeg / ffprobe](https://ffmpeg.org/download.html) | **Yes** | Probes title metadata and splits combined episodes. |
| [TMDB API key](https://www.themoviedb.org/settings/api) | **Yes** | Identifies discs. Free for personal use. |
| [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) | Optional | Reads DVD menus when the disc label is useless. |

Auto-Ripper **does not redistribute MakeMKV or ffmpeg**. It detects them on
first run and tells you what's missing.

## Install

Download the latest Windows installer from
[Releases](https://github.com/VanWardawg/auto-dvd-rippr/releases) and run it.

> **SmartScreen warning:** releases are not code-signed yet, so Windows will
> show "Windows protected your PC". Click **More info → Run anyway**. Verify
> the download against `SHA256SUMS.txt` from the same release if you want to be
> careful.

On first launch, Auto-Ripper walks you through pointing it at MakeMKV, ffmpeg,
your TMDB key, and your staging and NAS folders. It auto-detects the tool paths
where it can.

## Usage

1. Pick the drive and tell Auto-Ripper whether the disc is a **movie** or **TV**.
2. Insert the disc and hit start — or turn on **continuous mode** and it will
   start automatically each time you load a new disc.
3. Watch progress. If identification or episode mapping is ambiguous, the job
   pauses and asks; otherwise it runs straight through to your NAS.

Configuration lives in `%APPDATA%\Auto-Ripper\config.json`, editable from the
in-app Settings tab.

### Command line

The desktop app is a front end over a full CLI, which is useful for debugging
and scripting:

```bash
py -3.11 app/main.py --config app/config.json validate-config
```

See [`wiki/usage-cli.md`](wiki/usage-cli.md) for the full command reference.

## Building from source

```bash
git clone https://github.com/VanWardawg/auto-dvd-rippr.git
```

You need Node 20+, Rust (stable), and Python 3.11.

```bash
py -3.11 -m pip install -r requirements-dev.txt
```

```bash
cd frontend && npm ci
```

Run in development mode — this uses `app/main.py` directly, so backend edits
take effect without a rebuild:

```bash
npm run tauri -- dev
```

Build a release installer:

```bash
npm run build:self-contained && npm run tauri -- build
```

Run the backend tests:

```bash
py -3.11 -m unittest discover -s app/tests -t app/tests
```

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — architecture and conventions, for contributors and AI assistants
- [`wiki/`](wiki/) — setup, CLI usage, debugging, testing
- [`specs/`](specs/) — design specifications
- [`tasks/`](tasks/) — implementation task breakdown

## Legal

Auto-Ripper is a workflow tool. It contains no decryption code and ships no
third-party ripping software — MakeMKV does that work, under its own license.
You are responsible for ensuring that ripping a given disc is lawful where you
live. This project is intended for making personal backups of media you own.

## License

[Apache License 2.0](LICENSE).

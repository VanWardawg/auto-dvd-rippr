# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/VanWardawg/auto-dvd-rippr/security/advisories/new)
rather than opening a public issue.

## What Auto-Ripper touches

Worth knowing when judging the risk of an issue:

- **Your TMDB API key** lives in `%APPDATA%\Auto-Ripper\config.json` in plain
  text, readable by anything running as your user. It is a free, read-only,
  personal-use key; treat it as low value but do not paste it into a bug report.
- **The network.** By default the app talks to exactly two places:
  `api.themoviedb.org` for identification, and the MakeMKV forum page it reads
  to check when the public beta key expires. If you set a notification webhook
  URL, it also POSTs to that URL -- the disc label, the job's status and a
  one-line message -- when a job needs you, fails, or finishes. Nothing else,
  and nothing is sent anywhere you did not configure.
- **Your notification webhook URL**, if you set one, is a credential: anyone
  holding it can trigger whatever it is wired to. It is stored in plain text
  in the same `config.json`, is never written to a log or a job record, and
  only `http://` and `https://` URLs are accepted. Do not paste it into a bug
  report.
- **External executables.** MakeMKV, ffmpeg, ffprobe and optionally tesseract
  are launched from the paths in your config. Those paths are entered by you
  in Settings; the app does not download or update them.
- **The filesystem.** It reads optical drives, writes to your staging folder,
  and writes to your NAS path. It deletes staged files when you clear them or
  when `clear_local_after_transfer` is on.

## What it deliberately does not do

- It ships no decryption code and bundles no third-party ripping software.
- It has no auto-update mechanism, no telemetry, and no analytics.
- The Python backend uses only the standard library, so its dependency surface
  is Python itself.

## Unsigned releases

Release installers are not code-signed, so Windows SmartScreen will warn about
them. Verify downloads against the `SHA256SUMS.txt` published with each
release. If a build ever fails to match, please report it.

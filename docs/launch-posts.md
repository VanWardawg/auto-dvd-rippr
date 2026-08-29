# Launch posts — drafts for review

Post only after v0.1.0 is public and the release page is live, so every link
works. Each is tailored to its community's culture; check the subreddit's
self-promo rules pinned in the sidebar on posting day.

---

## r/Plex

**Title:** I built a free, open-source tool that automates disc → Plex: rip, identify, name, copy to NAS

**Body:**

Like a lot of you I have a wall of DVDs and Blu-rays and a Plex server, and
the gap between them was hours of MakeMKV + spreadsheet + hand-renaming per
season. So I built Auto-Ripper: put a disc in, it identifies it against TMDB,
rips just the titles worth keeping (skipping trailers/logos/play-all tracks),
matches episodes by reading the DVD menus and title cards with OCR, applies
Plex naming (`Show (Year)/Season 01/Show - s01e02 - Title.mkv`), and copies
to the NAS with checksums. Anything it isn't sure about pauses and asks
instead of guessing — a wrong auto-pick that lands on your server is worse
than a question.

It's Windows-only for now, free and open source (Apache 2.0). It drives your
existing MakeMKV/ffmpeg installs rather than bundling anything.

GitHub + installer: https://github.com/VanWardawg/auto-dvd-rippr

Fair warning: it's a 0.1.0 built by ripping my own kids-movie shelf, so
expect rough edges — bug reports very welcome.

---

## r/selfhosted

**Title:** Auto-Ripper — open-source automated DVD/Blu-ray → media library pipeline (Windows, drives MakeMKV)

**Body:**

I've been migrating a large disc collection to my NAS and got tired of the
manual loop, so I automated the whole pipeline and open-sourced it:

- Identifies discs against TMDB (with DVD-menu OCR fallback for useless disc labels)
- Rips only the worthwhile titles via MakeMKV
- Maps ripped files to episodes, splits combined-episode titles on chapter boundaries
- Plex-convention naming, checksummed copy to the NAS, resumable at every stage
- Pauses for one-click human review whenever confidence is low, instead of guessing

Stack, if you care: Tauri 2 + React shell over a stdlib-only Python backend,
SQLite as both state store and IPC. No telemetry, no accounts, config is one
JSON file. Windows-only today.

https://github.com/VanWardawg/auto-dvd-rippr

---

## r/DataHoarder

**Title:** Open-sourced my disc-backup automation: MakeMKV + TMDB + OCR pipeline with per-file checksums

**Body:**

Backing up a few hundred DVDs/Blu-rays taught me the bottleneck isn't the
ripping — it's everything around it. Measured over my own first 150 discs:
~48 hours of ripping vs ~107 hours of waiting on a human to identify, name,
and file things. So I automated the human part where it's automatable and
made the rest one-click review.

Things this crowd might appreciate: it verifies NAS copies with SHA-256
during the copy (not a read-back after), refuses to overwrite existing
destinations, keeps every stage resumable so a mid-rip failure never costs
finished work, and when a worn disc carries the feature twice it retries the
other copy before giving up.

Windows, free, Apache 2.0, drives your existing MakeMKV: 
https://github.com/VanWardawg/auto-dvd-rippr

---

## MakeMKV forum (Third-Party Tools / General)

**Title:** Auto-Ripper — open-source Windows GUI that automates makemkvcon → Plex-named library

**Body:**

I've built a free, open-source tool on top of makemkvcon and figured this
forum should see it. Auto-Ripper automates the full disc-to-library loop:
robot-mode scan, title selection (drops trailers/logos/play-all aggregates by
duration arithmetic), streaming rip with live progress parsed from PRGV
lines, TMDB identification, episode mapping via menu/title-card OCR, Plex
naming, and a checksummed NAS copy.

Notes for MakeMKV users specifically: it keeps `--minlength` identical
between scan and rip so title numbering stays stable; it detects beta-key
expiry and warns before rips start failing; and it treats "0 titles saved"
with exit code 0 as the failure it is. It never bundles or redistributes
MakeMKV — it finds your installed copy and asks you to keep it licensed.

Source and installer: https://github.com/VanWardawg/auto-dvd-rippr

Happy to answer anything about the robot-output handling — parsing PRGV/TINFO
taught me a few things I'd have loved to find on this forum.

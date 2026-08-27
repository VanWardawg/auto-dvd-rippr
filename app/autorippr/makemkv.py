"""
Parsing and title selection for MakeMKV's robot (`-r`) interface.

Kept separate from rip.py so the decision logic -- which titles are worth
ripping, and what MakeMKV is telling us about progress -- can be unit-tested
without a disc in the drive.

MakeMKV robot output is CSV after a `PREFIX:` marker, with quoted strings:

    TINFO:0,9,0,"1:23:45"          per-title attribute
    PRGV:12345,30000,65536         progress: current, total, max
    PRGC:5057,0,"Analyzing seamless segments"   current operation
    PRGT:5018,0,"Saving all titles to MKV"     total operation
"""

import csv
from dataclasses import dataclass, field
from io import StringIO
from typing import Any, Iterable


# TINFO attribute codes we care about, from MakeMKV's apdefs.h.
ATTR_NAME = 2
ATTR_CHAPTER_COUNT = 8
ATTR_DURATION = 9
ATTR_DISK_SIZE = 10
ATTR_DISK_SIZE_BYTES = 11
ATTR_SOURCE_FILE_NAME = 16
ATTR_SEGMENT_COUNT = 25
ATTR_SEGMENT_MAP = 26
ATTR_OUTPUT_FILE_NAME = 27

# The progress scale MakeMKV reports against. It sends this as the third PRGV
# field, but defaults are useful when a line arrives malformed.
PROGRESS_MAX = 65536


@dataclass(frozen=True)
class TitleCandidate:
    """One title MakeMKV found on the disc, before we decide whether to rip it."""

    title_id: int
    duration_seconds: float
    size_bytes: int
    chapter_count: int
    segment_count: int
    name: str

    @property
    def duration_minutes(self) -> float:
        return self.duration_seconds / 60.0


def parse_duration(value: str) -> float:
    """Parse MakeMKV's "h:mm:ss" or "mm:ss" duration into seconds."""
    parts = [p.strip() for p in str(value).split(":") if p.strip() != ""]
    if not parts:
        return 0.0
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return 0.0
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60.0 + number
    return seconds


def _parse_size_bytes(fields: dict[str, str]) -> int:
    raw = fields.get(str(ATTR_DISK_SIZE_BYTES)) or ""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _parse_int(fields: dict[str, str], code: int) -> int:
    raw = fields.get(str(code)) or ""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def build_title_candidates(per_title: dict[int, dict[str, Any]]) -> list[TitleCandidate]:
    """Turn the raw TINFO field map from a disc scan into typed candidates."""
    candidates: list[TitleCandidate] = []
    for title_id, entry in sorted(per_title.items()):
        fields = entry.get("fields", {}) or {}
        candidates.append(
            TitleCandidate(
                title_id=int(title_id),
                duration_seconds=parse_duration(fields.get(str(ATTR_DURATION)) or ""),
                size_bytes=_parse_size_bytes(fields),
                chapter_count=_parse_int(fields, ATTR_CHAPTER_COUNT),
                segment_count=_parse_int(fields, ATTR_SEGMENT_COUNT),
                name=str(
                    entry.get("display_name")
                    or fields.get(str(ATTR_NAME))
                    or fields.get(str(ATTR_SOURCE_FILE_NAME))
                    or f"Title {title_id}"
                ),
            )
        )
    return candidates


@dataclass(frozen=True)
class TitleSelection:
    """Which titles to rip, and why -- the reason is surfaced in the job log."""

    title_ids: list[int]
    reason: str
    skipped: list[str]
    # Selected title -> the same-length titles collapsed into it, best first.
    # A disc often carries two copies of the feature on different sectors, so
    # when the chosen one hits unreadable media the twin is worth a try.
    alternates: dict[int, list[int]] = field(default_factory=dict)

    @property
    def is_everything(self) -> bool:
        return not self.skipped


def select_titles(
    candidates: list[TitleCandidate],
    *,
    media_type: str,
    movie_mode: str = "single",
    min_episode_minutes: float = 10.0,
    max_episode_minutes: float = 90.0,
) -> TitleSelection:
    """
    Decide which titles are worth ripping.

    Ripping every title MakeMKV reports means ripping studio logos, trailers,
    language variants, and "play all" tracks that duplicate content already
    captured episode by episode -- often more data than the content itself.

    Returns every candidate when the disc gives no reason to exclude anything,
    so the caller can take the faster single-pass `all` path.
    """
    if not candidates:
        return TitleSelection(title_ids=[], reason="No titles reported by MakeMKV.", skipped=[])

    if media_type == "movie":
        return _select_movie_titles(candidates, movie_mode=movie_mode)
    return _select_tv_titles(
        candidates,
        min_episode_minutes=min_episode_minutes,
        max_episode_minutes=max_episode_minutes,
    )


def _required_movie_slots(movie_mode: str) -> int:
    return {"single": 1, "double_feature": 2, "trilogy": 3}.get(movie_mode, 1)


def _select_movie_titles(candidates: list[TitleCandidate], *, movie_mode: str) -> TitleSelection:
    wanted = _required_movie_slots(movie_mode)
    longest = max(c.duration_seconds for c in candidates)
    if longest <= 0:
        return TitleSelection(
            title_ids=[c.title_id for c in candidates],
            reason="Durations unavailable; ripping all titles.",
            skipped=[],
        )

    # A feature is within striking distance of the longest title. Extras and
    # trailers fall far below it. 60% keeps a double feature's shorter half
    # while dropping a 4-minute gag reel.
    threshold = longest * 0.60
    features = [c for c in candidates if c.duration_seconds >= threshold]

    # Blu-ray playlist obfuscation produces many near-identical long titles.
    # Collapse titles of the same duration to the largest one, which is the
    # complete version rather than a partial angle.
    #
    # Size is only a tiebreak between equals, though -- it says nothing about
    # which copy is physically readable. A worn DVD carrying the feature twice
    # can have the larger copy land on scratched sectors, so the runners-up are
    # kept as fallbacks rather than thrown away.
    by_duration: dict[int, list[TitleCandidate]] = {}
    for candidate in sorted(features, key=lambda c: (-c.size_bytes, c.title_id)):
        # Round to 5s so trivially different variants of one feature collapse.
        key = int(round(candidate.duration_seconds / 5.0))
        by_duration.setdefault(key, []).append(candidate)

    deduped = sorted(
        (group[0] for group in by_duration.values()),
        key=lambda c: (-c.duration_seconds, c.title_id),
    )
    selected = sorted(deduped[:wanted], key=lambda c: c.title_id)
    selected_ids = {c.title_id for c in selected}

    alternates = {
        group[0].title_id: [c.title_id for c in group[1:]]
        for group in by_duration.values()
        if group[0].title_id in selected_ids and len(group) > 1
    }

    skipped = [
        f"title {c.title_id} ({c.duration_minutes:.0f} min, {c.name})"
        for c in candidates
        if c.title_id not in selected_ids
    ]
    if not skipped:
        return TitleSelection(
            title_ids=[c.title_id for c in candidates],
            reason=f"All {len(candidates)} title(s) look like feature content.",
            skipped=[],
        )
    return TitleSelection(
        title_ids=[c.title_id for c in selected],
        alternates=alternates,
        reason=(
            f"Selected {len(selected)} feature title(s) of {len(candidates)}; "
            f"longest is {longest / 60:.0f} min."
        ),
        skipped=skipped,
    )


def _select_tv_titles(
    candidates: list[TitleCandidate],
    *,
    min_episode_minutes: float,
    max_episode_minutes: float,
) -> TitleSelection:
    episodes = [
        c for c in candidates
        if min_episode_minutes <= c.duration_minutes <= max_episode_minutes
    ]

    # A "play all" title runs roughly as long as the episodes combined. It
    # duplicates content we are already capturing per episode, and on a
    # 4-episode disc it doubles the rip.
    play_all_ids = _identify_play_all_titles(candidates, episodes)

    # Combined titles ("two episodes back to back") exceed the per-episode
    # window but are not play-all tracks. Keep them: the splitter handles them.
    combined = [
        c for c in candidates
        if c.duration_minutes > max_episode_minutes and c.title_id not in play_all_ids
    ]

    keep = sorted(
        {c.title_id for c in episodes} | {c.title_id for c in combined}
    )

    if not keep:
        # Nothing matched the expected shape -- an unusual disc, or bad config.
        # Rip everything rather than silently producing an empty job.
        return TitleSelection(
            title_ids=[c.title_id for c in candidates],
            reason=(
                f"No title fell in the {min_episode_minutes:.0f}-{max_episode_minutes:.0f} min "
                "episode window; ripping all titles."
            ),
            skipped=[],
        )

    keep_set = set(keep)
    skipped = [
        f"title {c.title_id} ({c.duration_minutes:.0f} min, {c.name})"
        + (" [play-all]" if c.title_id in play_all_ids else "")
        for c in candidates
        if c.title_id not in keep_set
    ]
    if not skipped:
        return TitleSelection(
            title_ids=keep,
            reason=f"All {len(candidates)} title(s) look like episode content.",
            skipped=[],
        )
    return TitleSelection(
        title_ids=keep,
        reason=f"Selected {len(keep)} episode title(s) of {len(candidates)}.",
        skipped=skipped,
    )


def _identify_play_all_titles(
    candidates: list[TitleCandidate],
    episodes: list[TitleCandidate],
) -> set[int]:
    if len(episodes) < 2:
        return set()
    episode_total = sum(c.duration_seconds for c in episodes)
    if episode_total <= 0:
        return set()
    play_all: set[int] = set()
    for candidate in candidates:
        if candidate in episodes:
            continue
        # Within 10% of the combined episode runtime, and clearly longer than
        # any single episode.
        if abs(candidate.duration_seconds - episode_total) <= episode_total * 0.10:
            play_all.add(candidate.title_id)
    return play_all


@dataclass(frozen=True)
class ProgressEvent:
    """One parsed line of MakeMKV robot progress output."""

    kind: str  # "values" | "current_op" | "total_op" | "message"
    current: int = 0
    total: int = 0
    maximum: int = PROGRESS_MAX
    text: str = ""


def parse_progress_line(line: str) -> ProgressEvent | None:
    """
    Parse one robot-mode line into a progress event, or None if it is not one.

    MakeMKV already reports exact progress on stdout; the previous
    implementation ignored it and inferred progress by polling the output
    directory's size every 15 seconds against a regex-scraped size estimate.
    """
    stripped = line.strip()
    if ":" not in stripped:
        return None
    prefix, _, payload = stripped.partition(":")
    if prefix not in ("PRGV", "PRGC", "PRGT", "MSG"):
        return None

    try:
        row = next(csv.reader(StringIO(payload)))
    except (StopIteration, csv.Error):
        return None

    if prefix == "PRGV":
        if len(row) < 3:
            return None
        try:
            current, total, maximum = (int(float(row[0])), int(float(row[1])), int(float(row[2])))
        except (TypeError, ValueError):
            return None
        return ProgressEvent(
            kind="values",
            current=current,
            total=total,
            maximum=maximum if maximum > 0 else PROGRESS_MAX,
        )

    if prefix in ("PRGC", "PRGT"):
        # code, id, name
        if len(row) < 3:
            return None
        return ProgressEvent(
            kind="current_op" if prefix == "PRGC" else "total_op",
            text=str(row[2]).strip(),
        )

    # MSG:code,flags,count,message,format,params...
    if len(row) < 4:
        return None
    return ProgressEvent(kind="message", text=str(row[3]).strip())


def overall_fraction(event: ProgressEvent) -> float | None:
    """Completed fraction of the whole operation from a PRGV event."""
    if event.kind != "values" or event.maximum <= 0:
        return None
    return min(1.0, max(0.0, event.total / event.maximum))


def format_titles(candidates: Iterable[TitleCandidate]) -> str:
    return ", ".join(
        f"#{c.title_id} {c.duration_minutes:.0f}min" for c in candidates
    )

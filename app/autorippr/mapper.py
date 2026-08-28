import json
import os
import re
import shutil
import subprocess
import sys
import time
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .dvdnav_menu import extract_dvdnav_menu_artifacts
from .rip import discover_optical_drives
from .state import append_job_log
from .tmdb import TmdbError, fetch_tmdb_tv_episodes


class MappingError(RuntimeError):
    pass


# How far outside the stated disc range to keep looking. Two episodes covers
# the usual cause -- a set that does not divide evenly -- without reopening the
# whole season to a mismatch.
RANGE_SLACK_EPISODES = 2


@dataclass(frozen=True)
class EpisodeTarget:
    episode_number: int
    tmdb_episode_id: int
    title: str
    # Which season this episode belongs to. Every episode on a normal disc
    # shares one, but a compilation draws from across the show, and
    # episode_mappings has always stored the season per row -- it was simply
    # given the same value every time.
    season_number: int = 1
    # Whether this episode is inside the user's stated disc range, as opposed
    # to the slack window around it. Slack episodes exist so a name match can
    # correct a near-miss range; they must never be handed out positionally.
    # When they counted the same as core episodes, a five-title disc with a
    # nine-episode window failed every count check and fell into the duration
    # heuristic, which read each 24-minute episode as a double bill.
    in_core_range: bool = True



def _job_disc_drive(conn, job_id: str) -> str | None:
    """The drive this job's disc was read from, if it is recorded."""
    row = conn.execute("SELECT optical_drive FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    drive = str(row["optical_drive"] or "").strip()
    return drive or None


def _job_disc_root(expected_drive: str | None) -> Path | None:
    """
    The disc root for this job, and only for this job.

    Menu and OCR capture used to take the first drive with a disc in it,
    whoever that disc belonged to. Once a job ejects its own disc -- which the
    TV path does as soon as the rip finishes, to free the drive -- that meant
    reading whatever was in the other bay. A Minnie's Pet Salon job ran OCR
    against the disc being ripped in F:, which was both the wrong film's menu
    and a drive already saturated by an active rip; ffmpeg timed out after five
    minutes and took the job down with it.

    Returning None is the right answer when the disc is gone. OCR then falls
    back to the ripped title file, which is local, fast, and unambiguously the
    right content.
    """
    if not expected_drive:
        return None
    wanted = expected_drive.strip().rstrip("\\").upper()
    for drive in discover_optical_drives():
        if not drive.get("has_media"):
            continue
        letter = str(drive.get("drive") or "").strip().rstrip("\\").upper()
        if letter == wanted:
            return Path(str(drive["root"]))
    return None


def analyze_dvd_menu(conn, cfg: AppConfig, job_id: str) -> dict[str, Any]:
    job = conn.execute(
        """
        SELECT id, media_type, season_number, episode_range_start, episode_range_end
        FROM jobs
        WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    if not job:
        raise MappingError(f"Job not found: {job_id}")
    is_tv_job = str(job["media_type"] or "tv") == "tv"

    analysis_dir = Path(cfg.staging_root) / "jobs" / job_id / "menu_analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)

    # Generate expensive artifacts once here, not during normal mapping.
    # Only ever this job's own disc: once a job ejects, "the first drive with
    # media" is somebody else's disc, very possibly one mid-rip.
    disc_drive = _job_disc_drive(conn, job_id)
    vlc_nav_screenshots = _capture_vlc_menu_snapshots(cfg, job_id, generate=True, disc_drive=disc_drive)
    dvd_arch_pages = _capture_dvd_archaeology_menu_pages(cfg, job_id, generate=True, disc_drive=disc_drive)
    dvd_arch_crops = _capture_dvd_archaeology_button_crops(cfg, job_id, generate=True, disc_drive=disc_drive)
    dvdnav_crops = _capture_dvdnav_button_crops(cfg, job_id, generate=True, disc_drive=disc_drive)
    media_title_hints = _collect_media_title_hints(
        cfg,
        job_id,
        [
            ("dvd_arch_button_crops", dvd_arch_crops),
            ("dvdnav_button_crops", dvdnav_crops),
            ("dvd_arch_menu_pages", dvd_arch_pages),
            ("vlc_nav_screenshots", vlc_nav_screenshots),
        ],
    )

    menu_analysis = {
        "job_id": job_id,
        "vlc_nav_screenshots": _relative_paths(cfg.staging_root, vlc_nav_screenshots),
        "dvd_arch_menu_pages": _relative_paths(cfg.staging_root, dvd_arch_pages),
        "dvd_arch_button_crops": _relative_paths(cfg.staging_root, dvd_arch_crops),
        "dvdnav_button_crops": _relative_paths(cfg.staging_root, dvdnav_crops),
        "media_title_hints": media_title_hints,
    }
    (analysis_dir / "menu_analysis.json").write_text(
        json.dumps(menu_analysis, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )

    if is_tv_job:
        bundle_association = _build_bundle_association_artifact(conn, cfg, job_id)
    else:
        bundle_association = {
            "job_id": job_id,
            "bundles": [],
            "play_all": None,
            "confidence_gate": {"ok": True, "reason": "Not applicable for movie jobs"},
        }
    (analysis_dir / "bundle_association.json").write_text(
        json.dumps(bundle_association, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )

    append_job_log(
        conn,
        job_id,
        "INFO",
        (
            f"DVD menu analysis completed: "
            f"vlc={len(menu_analysis['vlc_nav_screenshots'])}, "
            f"dvd_arch_pages={len(menu_analysis['dvd_arch_menu_pages'])}, "
            f"dvd_arch_crops={len(menu_analysis['dvd_arch_button_crops'])}, "
            f"dvdnav_crops={len(menu_analysis['dvdnav_button_crops'])}, "
            f"title_hints={len(menu_analysis['media_title_hints'])}, "
            f"bundle_matches={len(bundle_association.get('bundles', []))}"
        ),
        None,
        None,
    )
    conn.commit()
    return {
        "job_id": job_id,
        "menu_analysis": menu_analysis,
        "bundle_association": bundle_association,
    }




def _season_for_row(
    row: dict[str, Any],
    season_by_episode_id: dict[int, int],
    default_season: int,
) -> int:
    """
    Which season a planned mapping belongs to, taken from the episodes it claims.

    episode_mappings has always stored a season per row; it was simply handed
    the same one every time. A compilation disc holds episodes from across a
    show, so each row needs the season of the episode it actually matched or
    the file is named into the wrong one.
    """
    for episode_id in row.get("tmdb_episode_ids") or []:
        season = season_by_episode_id.get(int(episode_id))
        if season is not None:
            return int(season)
    return default_season


def _fetch_compilation_episodes(
    conn,
    cfg: AppConfig,
    tmdb_show_id: int,
    *,
    include_specials: bool,
) -> list[dict[str, Any]]:
    """
    Every episode of a show, for a disc that draws from all over it.

    A themed DVD like "Minnie's Pet Salon" is not a season -- it is a handful
    of Mickey Mouse Clubhouse episodes picked for their subject, from wherever
    in the run they happened to air. There is no range to apply and no disc
    order to trust, so the whole show becomes the candidate set and the
    episodes are identified by name.

    Specials are included only when asked for. Mickey Mouse Clubhouse has 47
    of them against 123 episodes total, so pulling them in nearly doubles the
    search space -- worth it when the disc really does draw on them, and just
    more chances for a wrong name match when it does not.
    """
    detail = _cached_show_detail(conn, cfg, tmdb_show_id)
    season_numbers = [
        int(season.get("season_number"))
        for season in (detail.get("seasons") or [])
        if isinstance(season.get("season_number"), int)
    ]
    if not include_specials:
        season_numbers = [number for number in season_numbers if number != 0]

    episodes: list[dict[str, Any]] = []
    for number in sorted(season_numbers):
        for episode in fetch_tmdb_tv_episodes(conn, cfg, tmdb_show_id, number):
            enriched = dict(episode)
            enriched["season_number"] = number
            episodes.append(enriched)
    return episodes


def _cached_show_detail(conn, cfg: AppConfig, tmdb_show_id: int) -> dict[str, Any]:
    from .tmdb import fetch_tv_show_seasons

    return fetch_tv_show_seasons(conn, cfg, tmdb_show_id)


def map_job_episodes(conn, cfg: AppConfig, job_id: str) -> dict[str, Any]:
    selected = conn.execute(
        """
        SELECT
            jsm.media_type,
            jsm.tmdb_id,
            jsm.title,
            jsm.year,
            jsm.season_number,
            jsm.order_mode,
            j.disc_scope,
            j.include_specials,
            j.episode_range_start,
            j.episode_range_end
        FROM job_selected_media jsm
        JOIN jobs j ON j.id = jsm.job_id
        WHERE jsm.job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if not selected:
        raise MappingError("No selected TMDB media found for job. Run tmdb identify/select first.")

    media_type = str(selected["media_type"])
    if media_type != "tv":
        raise MappingError("MVP mapping currently supports TV only.")

    season_number = selected["season_number"] if selected["season_number"] is not None else 1
    disc_scope_early = str(selected["disc_scope"] or "")
    if disc_scope_early == "compilation":
        episodes = _fetch_compilation_episodes(
            conn,
            cfg,
            int(selected["tmdb_id"]),
            include_specials=bool(selected["include_specials"]),
        )
        if not episodes:
            raise TmdbError(f"No TMDB episodes found for show={selected['tmdb_id']}")
    else:
        episodes = fetch_tmdb_tv_episodes(conn, cfg, int(selected["tmdb_id"]), int(season_number))
        if not episodes:
            raise TmdbError(f"No TMDB episodes found for show={selected['tmdb_id']} season={season_number}")

    rip_rows = conn.execute(
        """
        SELECT id, title_id, duration_seconds, chapter_count, source_file, raw_metadata_json
        FROM rip_titles
        WHERE job_id = ?
        ORDER BY title_id ASC, id ASC
        """,
        (job_id,),
    ).fetchall()
    if not rip_rows:
        raise MappingError("No rip titles found for job. Run rip first.")

    targets = [
        EpisodeTarget(
            episode_number=int(e["episode_number"]),
            tmdb_episode_id=int(e["id"]),
            title=str(e["name"]),
            season_number=int(e.get("season_number", season_number) or season_number),
        )
        for e in episodes
    ]
    disc_scope = str(selected["disc_scope"] or "")
    range_start = int(selected["episode_range_start"]) if selected["episode_range_start"] is not None else None
    range_end = int(selected["episode_range_end"]) if selected["episode_range_end"] is not None else None
    if disc_scope == "partial_season" and range_start is not None and range_end is not None:
        # The range is the user's estimate of which episodes are on this disc,
        # and it is routinely a little off: an Avatar season set suggested 7-11
        # for disc 2 because the arithmetic assumed an even split, when disc 1
        # actually held five episodes and disc 2 began at 6. Filtering to the
        # stated range exactly made the true answer unreachable -- E06 was not
        # among the candidates, so no amount of name matching could find it,
        # and every episode on the disc came out one too high.
        #
        # Widening the window lets the name match correct a near-miss while
        # still keeping the list short enough to be useful.
        low = max(1, range_start - RANGE_SLACK_EPISODES)
        high = range_end + RANGE_SLACK_EPISODES
        widened = [
            EpisodeTarget(
                episode_number=t.episode_number,
                tmdb_episode_id=t.tmdb_episode_id,
                title=t.title,
                season_number=t.season_number,
                in_core_range=range_start <= t.episode_number <= range_end,
            )
            for t in targets
            if low <= t.episode_number <= high
        ]
        targets = widened or [
            t for t in targets if range_start <= t.episode_number <= range_end
        ]
        if not targets:
            raise MappingError(
                f"No TMDB episodes remain after applying disc range {range_start}-{range_end}."
            )

    _clear_downstream_state_for_remap(conn, cfg, job_id)
    planned = _plan_mappings(
        rip_rows, targets, cfg, job_id, _job_disc_drive(conn, job_id),
        position_is_evidence=disc_scope_early != "compilation",
    )
    # Rows are built at several points in the planner, so the season is derived
    # here from the episodes each row actually claims rather than threaded
    # through every one of them. For a normal disc every target shares a
    # season and this is a no-op; for a compilation it is the whole point.
    season_by_episode_id = {t.tmdb_episode_id: t.season_number for t in targets}
    conn.execute("DELETE FROM episode_mappings WHERE job_id = ?", (job_id,))
    for row in planned:
        conn.execute(
            """
            INSERT INTO episode_mappings (
                job_id, rip_title_id, season_number, episode_start, episode_end,
                tmdb_episode_ids_json, episode_titles_json, confidence, reason, manual_override, needs_split
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                row["rip_title_id"],
                _season_for_row(row, season_by_episode_id, int(season_number)),
                row["episode_start"],
                row["episode_end"],
                json.dumps(row["tmdb_episode_ids"], ensure_ascii=True),
                json.dumps(row["episode_titles"], ensure_ascii=True),
                row["confidence"],
                row["reason"],
                0,
                1 if row["needs_split"] else 0,
            ),
        )

    needs_review = any(
        (p["episode_start"] is not None and float(p["confidence"]) < 0.85)
        for p in planned
    )
    if disc_scope_early == "compilation" and planned:
        # On an ordinary disc, position is evidence: title 3 is usually episode
        # 3. On a compilation it is nothing at all -- the episodes were picked
        # for their subject from anywhere in the show, and the candidate pool
        # is the whole run. When the name match fails, the planner still falls
        # back to assigning whatever is next in the pool, which for Mickey
        # Mouse Clubhouse means the specials, in the order TMDB returned them.
        # That is a guess that looks exactly like an answer, so these are
        # always confirmed by hand.
        needs_review = True
        append_job_log(
            conn,
            job_id,
            "INFO",
            "Compilation disc: episode order on the disc carries no meaning, so "
            "every assignment is offered for confirmation rather than applied.",
            None,
            None,
        )
    append_job_log(
        conn,
        job_id,
        "WARNING" if needs_review else "INFO",
        (
            f"Episode mapping completed: {len(planned)} mapping rows. "
            f"{'Manual review recommended.' if needs_review else 'No immediate review flags.'}"
        ),
        None,
        None,
    )
    conn.commit()
    return {
        "job_id": job_id,
        "season_number": int(season_number),
        "mapping_count": len(planned),
        "needs_review": needs_review,
        "mappings": planned,
    }


def _clear_downstream_state_for_remap(conn, cfg: AppConfig, job_id: str) -> None:
    output_rows = conn.execute(
        "SELECT id FROM outputs WHERE job_id = ?",
        (job_id,),
    ).fetchall()
    output_ids = [int(r["id"]) for r in output_rows]
    for output_id in output_ids:
        conn.execute("DELETE FROM transfer_attempts WHERE output_id = ?", (output_id,))

    conn.execute("DELETE FROM outputs WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM finalized_manifests WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM split_plans WHERE job_id = ?", (job_id,))

    job_root = Path(cfg.staging_root) / "jobs" / job_id
    for subdir in ("split_output", "finalized"):
        path = job_root / subdir
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def set_mapping_override(
    conn,
    cfg: AppConfig,
    mapping_id: int,
    episode_start: int,
    episode_end: int,
    tmdb_episode_ids: list[int],
    reason: str,
    season_number: int | None = None,
) -> dict[str, Any]:
    """
    Correct one title-to-episode assignment by hand.

    `season_number` matters on a compilation, where the disc's episodes come
    from all over the show. Without it the season was read from
    job_selected_media -- which a compilation leaves NULL, so it fell back to
    season 1 and looked the episode title up there, attaching a season 1 name
    to a row sitting in season 3. Passing None keeps the row's own season,
    which is the right answer for an ordinary disc.
    """
    row = conn.execute(
        "SELECT id, job_id, season_number FROM episode_mappings WHERE id = ?",
        (mapping_id,),
    ).fetchone()
    if not row:
        raise MappingError(f"Mapping not found: {mapping_id}")
    selected_media = conn.execute(
        """
        SELECT tmdb_id, season_number
        FROM job_selected_media
        WHERE job_id = ?
        LIMIT 1
        """,
        (row["job_id"],),
    ).fetchone()
    if season_number is None:
        # The row already knows its season; only fall back when it does not.
        if row["season_number"] is not None:
            season_number = int(row["season_number"])
        elif selected_media and selected_media["season_number"] is not None:
            season_number = int(selected_media["season_number"])
        else:
            season_number = 1
    season_number = int(season_number)
    title_lookup: dict[int, str] = {}
    if selected_media:
        episodes = fetch_tmdb_tv_episodes(conn, cfg, int(selected_media["tmdb_id"]), season_number)
        title_lookup = {int(ep["episode_number"]): str(ep["name"]) for ep in episodes}
    episode_titles = [
        title_lookup.get(n, f"Episode {n}")
        for n in range(episode_start, episode_end + 1)
    ]
    conn.execute(
        """
        UPDATE episode_mappings
        SET season_number = ?, episode_start = ?, episode_end = ?, tmdb_episode_ids_json = ?,
            episode_titles_json = ?, reason = ?, manual_override = 1,
            needs_split = ?, confidence = ?
        WHERE id = ?
        """,
        (
            season_number,
            episode_start,
            episode_end,
            json.dumps(tmdb_episode_ids, ensure_ascii=True),
            json.dumps(episode_titles, ensure_ascii=True),
            reason,
            1 if episode_end > episode_start else 0,
            1.0,
            mapping_id,
        ),
    )
    append_job_log(
        conn,
        row["job_id"],
        "INFO",
        f"Mapping override set on mapping_id={mapping_id} to "
        f"S{season_number:02d}E{episode_start}-E{episode_end}",
        None,
        None,
    )
    conn.commit()
    return {
        "mapping_id": mapping_id,
        "season_number": season_number,
        "episode_start": episode_start,
        "episode_end": episode_end,
        "tmdb_episode_ids": tmdb_episode_ids,
    }


def set_mapping_source_override(
    conn,
    mapping_id: int,
    rip_title_id: int,
    reason: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT em.id, em.job_id, rt.source_file
        FROM episode_mappings em
        JOIN rip_titles rt ON rt.id = ?
        WHERE em.id = ?
        """,
        (rip_title_id, mapping_id),
    ).fetchone()
    if not row:
        raise MappingError(
            f"Mapping or rip title not found for mapping_id={mapping_id}, rip_title_id={rip_title_id}"
        )
    conn.execute(
        """
        UPDATE episode_mappings
        SET rip_title_id = ?, manual_override = 1, confidence = 1.0, reason = ?
        WHERE id = ?
        """,
        (rip_title_id, reason, mapping_id),
    )
    append_job_log(
        conn,
        row["job_id"],
        "INFO",
        f"Mapping source override set on mapping_id={mapping_id} to rip_title_id={rip_title_id}",
        None,
        None,
    )
    conn.commit()
    return {
        "mapping_id": mapping_id,
        "rip_title_id": rip_title_id,
        "source_file": row["source_file"],
    }


def set_mapping_ignore(
    conn,
    mapping_id: int,
    reason: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT em.id, em.job_id, rt.source_file
        FROM episode_mappings em
        LEFT JOIN rip_titles rt ON rt.id = em.rip_title_id
        WHERE em.id = ?
        """,
        (mapping_id,),
    ).fetchone()
    if not row:
        raise MappingError(f"Mapping not found: {mapping_id}")

    conn.execute(
        """
        UPDATE episode_mappings
        SET episode_start = NULL,
            episode_end = NULL,
            tmdb_episode_ids_json = '[]',
            episode_titles_json = '[]',
            manual_override = 1,
            needs_split = 0,
            confidence = 1.0,
            reason = ?
        WHERE id = ?
        """,
        (reason, mapping_id),
    )
    append_job_log(
        conn,
        row["job_id"],
        "INFO",
        f"Mapping ignored on mapping_id={mapping_id}",
        None,
        None,
    )
    conn.commit()
    return {
        "mapping_id": mapping_id,
        "ignored": True,
        "source_file": row["source_file"],
    }




def _typical_episode_seconds(rows) -> float:
    """
    One episode's length on this disc, taken from the disc itself.

    The median of the full-length titles: on an episode disc most titles are
    single episodes, so the median is one episode even when a couple of titles
    are doubles. Falls back to 22 minutes only when the disc offers nothing to
    measure.
    """
    durations = sorted(
        float(r["duration_seconds"] or 0)
        for r in rows
        if float(r["duration_seconds"] or 0) >= 10 * 60
    )
    if not durations:
        return 22.0 * 60
    return durations[len(durations) // 2]


def _take_core_targets(remaining: list[EpisodeTarget], count: int) -> list[EpisodeTarget]:
    """
    Hand out the next `count` in-range episodes, removing them from `remaining`.

    Positional assignment must never reach into the slack window: those
    episodes are only there so a *name* match can correct a near-miss range.
    Falls back to whatever is left only when no core episodes remain, because
    returning nothing would crash the caller.
    """
    core = [t for t in remaining if t.in_core_range]
    pool = core if core else remaining
    taken = pool[:count] if pool else []
    for target in taken:
        remaining.remove(target)
    return taken


def _plan_mappings(
    rip_rows,
    targets: list[EpisodeTarget],
    cfg: AppConfig,
    job_id: str,
    disc_drive: str | None = None,
    position_is_evidence: bool = True,
) -> list[dict[str, Any]]:
    remaining = targets[:]
    output: list[dict[str, Any]] = []
    play_all_ids = _identify_likely_play_all_titles(rip_rows)
    bundle_assignments = _derive_cached_bundle_assignments(cfg, job_id, rip_rows, targets, play_all_ids)
    assigned_by_rip_id: dict[int, list[EpisodeTarget]] = {}
    for rip_id, assigned_targets in bundle_assignments.items():
        assigned_by_rip_id[int(rip_id)] = assigned_targets
    assigned_numbers = {t.episode_number for vals in assigned_by_rip_id.values() for t in vals}
    remaining = [t for t in remaining if t.episode_number not in assigned_numbers]
    unassigned_rows = [
        row for row in rip_rows
        if int(row["id"]) not in play_all_ids and int(row["id"]) not in assigned_by_rip_id
    ]
    core_remaining = [t for t in remaining if t.in_core_range]
    in_order_primary_rows = _select_in_order_primary_episode_rows(unassigned_rows, len(core_remaining))
    typical_episode_seconds = _typical_episode_seconds(unassigned_rows)
    in_order_primary_ids = {int(row["id"]) for row in in_order_primary_rows}

    for r in rip_rows:
        if int(r["id"]) in play_all_ids:
            output.append(
                {
                    "rip_title_id": int(r["id"]),
                    "episode_start": None,
                    "episode_end": None,
                    "tmdb_episode_ids": [],
                    "episode_titles": [],
                    "confidence": 0.95,
                    "reason": "Likely Play All / aggregate title; excluded from episode mapping.",
                    "needs_split": False,
                }
            )
            continue
        preassigned = assigned_by_rip_id.get(int(r["id"]))
        if preassigned:
            assigned = preassigned
            episodes_for_title = len(assigned)
            confidence = 0.93
            reason = (
                f"Assigned from cached menu bundle order using {Path(str(r['source_file'])).name} "
                f"and visible menu screenshots."
            )
            output.append(
                {
                    "rip_title_id": int(r["id"]),
                    "episode_start": assigned[0].episode_number,
                    "episode_end": assigned[-1].episode_number,
                    "tmdb_episode_ids": [a.tmdb_episode_id for a in assigned],
                    "episode_titles": [a.title for a in assigned],
                    "confidence": confidence,
                    "reason": reason,
                    "needs_split": episodes_for_title > 1,
                }
            )
            continue
        if in_order_primary_rows:
            if int(r["id"]) not in in_order_primary_ids:
                output.append(
                    {
                        "rip_title_id": int(r["id"]),
                        "episode_start": None,
                        "episode_end": None,
                        "tmdb_episode_ids": [],
                        "episode_titles": [],
                        "confidence": 0.96,
                        "reason": (
                            "Excluded as likely extra/alternate title because the disc contains a clear set of "
                            "full-length episode files that matches the requested episode count."
                        ),
                        "needs_split": False,
                    }
                )
                continue
            assigned = _take_core_targets(remaining, 1)
            episodes_for_title = 1
            output.append(
                {
                    "rip_title_id": int(r["id"]),
                    "episode_start": assigned[0].episode_number,
                    "episode_end": assigned[-1].episode_number,
                    "tmdb_episode_ids": [a.tmdb_episode_id for a in assigned],
                    "episode_titles": [a.title for a in assigned],
                    "confidence": 0.94 if int(r["chapter_count"] or 0) > 0 else 0.90,
                    "reason": (
                        "Assigned in disc order because the number of clear full-length episode files matches the "
                        "requested episode count, and the remaining titles are much shorter extras/alternates."
                    ),
                    "needs_split": False,
                }
            )
            continue
        if not remaining:
            # duplicate/alternate likely
            output.append(
                {
                    "rip_title_id": int(r["id"]),
                    "episode_start": None,
                    "episode_end": None,
                    "tmdb_episode_ids": [],
                    "episode_titles": [],
                    "confidence": 0.30,
                    "reason": "No remaining target episodes; likely duplicate/alternate title.",
                    "needs_split": False,
                }
            )
            continue

        raw = _parse_raw_metadata(r["raw_metadata_json"])
        menu_name = _extract_menu_name(raw)
        menu_match = _find_best_menu_match(menu_name, remaining) if menu_name else None
        ocr_match = None
        ocr_artifact_image = None
        ocr_artifact_text = None
        ocr_best_score = None
        attempted_ocr = False
        if not menu_match and _should_try_ocr_menu_fallback(menu_name):
            attempted_ocr = True
            ocr_result = _find_best_ocr_menu_match(
                disc_drive=disc_drive,
                cfg=cfg,
                job_id=job_id,
                source_file=str(r["source_file"]),
                rip_title_id=int(r["id"]),
                targets=remaining,
            )
            ocr_match = ocr_result.get("match")
            ocr_artifact_image = ocr_result.get("artifact_image_path")
            ocr_artifact_text = ocr_result.get("artifact_text_path")
            ocr_best_score = ocr_result.get("best_score")

        dur = float(r["duration_seconds"] or 0.0)
        if menu_match or ocr_match:
            # Prioritize explicit menu/title text when it strongly matches TMDB episode name.
            best_match = menu_match if menu_match else ocr_match
            start_idx = int(best_match["index"])
            match_indices = best_match.get("indices") if isinstance(best_match, dict) else None
            if (
                isinstance(match_indices, list)
                and match_indices
                and all(isinstance(i, int) for i in match_indices)
                and match_indices == list(range(match_indices[0], match_indices[-1] + 1))
            ):
                assigned = remaining[match_indices[0] : match_indices[-1] + 1]
                del remaining[match_indices[0] : match_indices[-1] + 1]
                episodes_for_title = len(assigned)
            else:
                assigned = remaining[start_idx : start_idx + 1]
                del remaining[start_idx : start_idx + 1]
                episodes_for_title = 1
        else:
            core_left = [t for t in remaining if t.in_core_range]
            if dur <= 0:
                episodes_for_title = 1
            else:
                # Measured against this disc's own typical title length, not a
                # hardcoded runtime. The old divisor was a flat 12 minutes,
                # which read every 24-minute episode as a double bill and set
                # needs_split on all of them -- the splitter would have cut
                # single episodes in half. A disc of uniform titles is its own
                # best evidence for what one episode looks like.
                episodes_for_title = max(1, int(round(dur / typical_episode_seconds)))
                episodes_for_title = min(4, episodes_for_title, max(1, len(core_left)))
            assigned = _take_core_targets(remaining, episodes_for_title)

        reason_parts = []
        if menu_match:
            reason_parts.append(
                f"Matched DVD menu title to episode '{assigned[0].title}' "
                f"(score={menu_match['score']:.2f})."
            )
        elif ocr_match:
            source_label = str(ocr_result.get("source_label") or "unknown source")
            if len(assigned) > 1:
                reason_parts.append(
                    f"OCR fallback matched on-screen text from {source_label} to combined episodes "
                    f"'{assigned[0].title}' through '{assigned[-1].title}' "
                    f"(score={ocr_match['score']:.2f})."
                )
            else:
                reason_parts.append(
                    f"OCR fallback matched on-screen text from {source_label} to episode "
                    f"'{assigned[0].title}' (score={ocr_match['score']:.2f})."
                )
        elif attempted_ocr:
            if ocr_best_score is not None:
                reason_parts.append(
                    f"OCR fallback could not find a confident episode title match "
                    f"(best_score={float(ocr_best_score):.2f})."
                )
            else:
                reason_parts.append("OCR fallback could not find a confident episode title match.")
        if ocr_artifact_image:
            reason_parts.append(f"OCR screenshot: {ocr_artifact_image}")
        if ocr_artifact_text:
            reason_parts.append(f"OCR text: {ocr_artifact_text}")
        if episodes_for_title > 1:
            reason_parts.append("Combined episodes inferred from long duration.")
        else:
            reason_parts.append("Single episode inferred from duration.")
        if int(r["title_id"] or 0) != (assigned[0].episode_number if assigned else 0):
            reason_parts.append("Potential out-of-order disc title sequence.")

        confidence = _confidence_for_assignment(
            duration_seconds=dur,
            episode_count=episodes_for_title,
            menu_match_score=(
                menu_match["score"] if menu_match else (ocr_match["score"] if ocr_match else None)
            ),
            position_is_evidence=position_is_evidence,
        )
        output.append(
            {
                "rip_title_id": int(r["id"]),
                "episode_start": assigned[0].episode_number,
                "episode_end": assigned[-1].episode_number,
                "tmdb_episode_ids": [a.tmdb_episode_id for a in assigned],
                "episode_titles": [a.title for a in assigned],
                "confidence": confidence,
                "reason": " ".join(reason_parts),
                "needs_split": episodes_for_title > 1,
            }
        )

    # Slack episodes that nothing claimed were never expected on this disc;
    # only unclaimed core episodes are worth reporting as missing.
    remaining = [t for t in remaining if t.in_core_range]
    if remaining:
        # unmatched episodes means unresolved mapping confidence drops globally
        missing = ", ".join(str(ep.episode_number) for ep in remaining)
        output.append(
            {
                "rip_title_id": None,
                "episode_start": remaining[0].episode_number,
                "episode_end": remaining[-1].episode_number,
                "tmdb_episode_ids": [ep.tmdb_episode_id for ep in remaining],
                "episode_titles": [ep.title for ep in remaining],
                "confidence": 0.20,
                "reason": f"Episodes {missing} have no mapped title.",
                "needs_split": False,
            }
        )
    return output


def _select_in_order_primary_episode_rows(rip_rows, target_count: int) -> list[Any]:
    if target_count <= 0 or len(rip_rows) < target_count:
        return []

    eligible_rows = [
        row for row in rip_rows
        if row["duration_seconds"] is not None and float(row["duration_seconds"]) >= 8 * 60
    ]
    if len(eligible_rows) < target_count:
        return []

    candidate_sets = [
        [row for row in eligible_rows if int(row["chapter_count"] or 0) > 0],
        eligible_rows,
    ]
    for candidate_rows in candidate_sets:
        if len(candidate_rows) != target_count:
            continue
        candidate_ids = {int(row["id"]) for row in candidate_rows}
        other_rows = [row for row in rip_rows if int(row["id"]) not in candidate_ids]
        candidate_durations = [float(row["duration_seconds"] or 0.0) for row in candidate_rows]
        other_durations = [float(row["duration_seconds"] or 0.0) for row in other_rows if row["duration_seconds"] is not None]
        if not candidate_durations:
            continue
        shortest_candidate = min(candidate_durations)
        longest_other = max(other_durations) if other_durations else 0.0
        if longest_other <= max(6 * 60, shortest_candidate * 0.70):
            return candidate_rows
    return []


def _derive_cached_bundle_assignments(
    cfg: AppConfig,
    job_id: str,
    rip_rows,
    targets: list[EpisodeTarget],
    play_all_ids: set[int],
) -> dict[int, list[EpisodeTarget]]:
    analysis_dir = Path(cfg.staging_root) / "jobs" / job_id / "menu_analysis"
    assoc_path = analysis_dir / "bundle_association.json"
    if assoc_path.exists():
        try:
            payload = json.loads(assoc_path.read_text(encoding="utf-8"))
            bundles = payload.get("bundles")
            if isinstance(bundles, list):
                by_episode = {t.episode_number: t for t in targets}
                loaded: dict[int, list[EpisodeTarget]] = {}
                for bundle in bundles:
                    if not isinstance(bundle, dict):
                        continue
                    rip_id = bundle.get("rip_title_id")
                    ep_nums = bundle.get("episode_numbers")
                    if not isinstance(rip_id, int) or not isinstance(ep_nums, list):
                        continue
                    vals = [by_episode[int(n)] for n in ep_nums if int(n) in by_episode]
                    if vals:
                        loaded[rip_id] = vals
                if loaded:
                    return loaded
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass

    nav_dir = Path(cfg.staging_root) / "jobs" / job_id / "ocr" / "vlc_nav"
    tesseract = _resolve_tesseract_executable()
    if tesseract is None or not nav_dir.exists():
        return {}

    files_by_group: dict[str, list[dict[str, Any]]] = {}
    for row in rip_rows:
        if int(row["id"]) in play_all_ids:
            continue
        stem = Path(str(row["source_file"] or "")).stem.upper()
        match = re.match(r"^([A-Z])(\d+)_T\d+$", stem)
        if not match:
            continue
        group = match.group(1)
        order_num = int(match.group(2))
        files_by_group.setdefault(group, []).append(
            {"rip_id": int(row["id"]), "order_num": order_num, "source_name": Path(str(row["source_file"])).name}
        )
    for group in files_by_group:
        files_by_group[group].sort(key=lambda x: x["order_num"])
    if not files_by_group:
        return {}

    images = sorted(nav_dir.glob("*.png"))
    if not images:
        return {}

    page_candidates: list[dict[str, Any]] = []
    for image in images:
        text = _ocr_frame_text(tesseract, image)
        if not text:
            continue
        groups = _extract_ordered_episode_groups_from_text(text, targets)
        if groups:
            avg_score = sum(float(g["score"]) for g in groups) / len(groups)
            page_candidates.append(
                {
                    "image": image,
                    "groups": groups,
                    "avg_score": avg_score,
                }
            )
    if not page_candidates:
        return {}

    assignments: dict[int, list[EpisodeTarget]] = {}
    used_images: set[Path] = set()
    for group_name, file_infos in sorted(files_by_group.items()):
        needed = len(file_infos)
        candidates = [
            p for p in page_candidates
            if p["image"] not in used_images and len(p["groups"]) == needed
        ]
        if not candidates:
            continue
        best_page = max(candidates, key=lambda p: p["avg_score"])
        used_images.add(best_page["image"])
        ordered_groups = best_page["groups"]
        for file_info, group_match in zip(file_infos, ordered_groups, strict=False):
            indices = group_match["indices"]
            assigned_targets = [targets[i] for i in indices if 0 <= i < len(targets)]
            if assigned_targets:
                assignments[int(file_info["rip_id"])] = assigned_targets
    return assignments


def _extract_ordered_episode_groups_from_text(
    ocr_text: str,
    targets: list[EpisodeTarget],
) -> list[dict[str, Any]]:
    matches = _find_episode_title_matches_in_text(ocr_text, targets)
    if not matches:
        return []
    groups: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = [matches[0]]
    for match in matches[1:]:
        if int(match["index"]) == int(current[-1]["index"]) + 1:
            current.append(match)
        else:
            groups.append(_build_group_match(current))
            current = [match]
    groups.append(_build_group_match(current))
    return [g for g in groups if g is not None]


def _build_group_match(group_matches: list[dict[str, Any]]) -> dict[str, Any]:
    indices = [int(m["index"]) for m in group_matches]
    avg_score = sum(float(m["score"]) for m in group_matches) / max(1, len(group_matches))
    return {
        "index": indices[0],
        "indices": indices,
        "score": avg_score,
        "episode": group_matches[0]["episode"],
    }


def _build_bundle_association_artifact(conn, cfg: AppConfig, job_id: str) -> dict[str, Any]:
    selected = conn.execute(
        """
        SELECT jsm.tmdb_id, jsm.season_number, j.episode_range_start, j.episode_range_end
        FROM job_selected_media jsm
        JOIN jobs j ON j.id = jsm.job_id
        WHERE jsm.job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if not selected:
        return {"job_id": job_id, "bundles": [], "play_all": None, "confidence_gate": {"ok": False, "reason": "No selected media"}}

    # Specials are season 0, which is falsy -- `or 1` would silently move a
    # specials disc into season 1.
    season_number = (
        int(selected["season_number"]) if selected["season_number"] is not None else 1
    )
    episodes = fetch_tmdb_tv_episodes(conn, cfg, int(selected["tmdb_id"]), season_number)
    targets = [
        EpisodeTarget(int(e["episode_number"]), int(e["id"]), str(e["name"]))
        for e in episodes
    ]
    start = int(selected["episode_range_start"]) if selected["episode_range_start"] is not None else None
    end = int(selected["episode_range_end"]) if selected["episode_range_end"] is not None else None
    if start is not None and end is not None:
        targets = [t for t in targets if start <= t.episode_number <= end]

    rip_rows = conn.execute(
        """
        SELECT id, title_id, duration_seconds, chapter_count, source_file, raw_metadata_json
        FROM rip_titles
        WHERE job_id = ?
        ORDER BY title_id ASC, id ASC
        """,
        (job_id,),
    ).fetchall()
    play_all_ids = _identify_likely_play_all_titles(rip_rows)
    bundle_assignments = _derive_cached_bundle_assignments(cfg, job_id, rip_rows, targets, play_all_ids)

    bundles: list[dict[str, Any]] = []
    low_confidence = False
    for rip_row in rip_rows:
        rip_id = int(rip_row["id"])
        if rip_id in play_all_ids:
            continue
        assigned = bundle_assignments.get(rip_id)
        if not assigned:
            low_confidence = True
            continue
        conf = 0.93
        bundles.append(
            {
                "rip_title_id": rip_id,
                "source_file": Path(str(rip_row["source_file"])).name,
                "episode_numbers": [t.episode_number for t in assigned],
                "episode_titles": [t.title for t in assigned],
                "confidence": conf,
            }
        )
        if conf < 0.85:
            low_confidence = True

    play_all = None
    if play_all_ids:
        for rip_row in rip_rows:
            if int(rip_row["id"]) in play_all_ids:
                play_all = {
                    "rip_title_id": int(rip_row["id"]),
                    "source_file": Path(str(rip_row["source_file"])).name,
                    "confidence": 0.95,
                }
                break
    return {
        "job_id": job_id,
        "bundles": bundles,
        "play_all": play_all,
        "confidence_gate": {
            "ok": not low_confidence and bool(bundles),
            "threshold": 0.85,
        },
    }


def _relative_paths(staging_root: str, files: list[Path]) -> list[str]:
    root = Path(staging_root)
    out: list[str] = []
    for f in files:
        try:
            out.append(str(f.relative_to(root)))
        except ValueError:
            out.append(str(f))
    return out


def _collect_media_title_hints(
    cfg: AppConfig,
    job_id: str,
    image_groups: list[tuple[str, list[Path]]],
    max_hints: int = 5,
) -> list[dict[str, str]]:
    tesseract = _resolve_tesseract_executable()
    if tesseract is None:
        return []

    root = Path(cfg.staging_root)
    seen: set[str] = set()
    hints: list[dict[str, str]] = []
    for source_name, image_files in image_groups:
        for image_file in image_files[:8]:
            text = _ocr_frame_text(tesseract, image_file)
            if not text:
                continue
            for candidate in _extract_title_hint_candidates_from_ocr(text):
                normalized = _normalize_name(candidate)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                try:
                    image_path = str(image_file.relative_to(root))
                except ValueError:
                    image_path = str(image_file)
                hints.append(
                    {
                        "text": candidate,
                        "normalized": normalized,
                        "source": source_name,
                        "image_path": image_path,
                    }
                )
                if len(hints) >= max_hints:
                    return hints
    return hints


def _extract_title_hint_candidates_from_ocr(ocr_text: str) -> list[str]:
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw_line in ocr_text.splitlines():
        for part in re.split(r"[|]+", raw_line):
            cleaned = _clean_title_hint_line(part)
            if not cleaned:
                continue
            normalized = _normalize_name(cleaned)
            if normalized in seen:
                continue
            seen.add(normalized)
            score = _score_title_hint_text(cleaned, normalized)
            if score <= 0:
                continue
            candidates.append((score, cleaned))
    candidates.sort(key=lambda item: (-item[0], -len(item[1]), item[1]))
    return [text for _, text in candidates[:3]]


def _clean_title_hint_line(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    text = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 5:
        return None
    normalized = _normalize_name(text)
    if not normalized:
        return None
    tokens = [token for token in normalized.split() if token]
    if not tokens:
        return None
    technical_tokens = {"disc", "disk", "dvd", "title", "menu", "chapter", "chapters", "track", "part", "play", "setup"}
    alpha_tokens = [token for token in tokens if re.search(r"[a-z]", token)]
    meaningful = [token for token in alpha_tokens if token not in technical_tokens and len(token) >= 3]
    if not meaningful:
        return None
    return text


def _score_title_hint_text(raw_text: str, normalized: str) -> int:
    tokens = [token for token in normalized.split() if token]
    alpha_count = sum(1 for token in tokens if re.search(r"[a-z]", token))
    long_count = sum(1 for token in tokens if len(token) >= 4)
    if alpha_count == 0 or long_count == 0:
        return 0
    return (long_count * 10) + min(len(raw_text), 80)


def _confidence_for_assignment(
    duration_seconds: float,
    episode_count: int,
    menu_match_score: float | None,
    position_is_evidence: bool = True,
) -> float:
    """
    How much to trust one title-to-episode assignment.

    Duration says how *big* a file is, never *which* episode it holds. On an
    ordinary disc position supplies the identity -- title 3 is episode 3 -- so
    a duration that fits is genuinely reassuring. On a compilation nothing
    supplies it, and a duration-derived 0.84 sat next to a real 0.97 name match
    in the review list looking equally settled, while its own reason line said
    "could not find a confident episode title match".
    """
    if menu_match_score is not None:
        if menu_match_score >= 0.90:
            return 0.97
        if menu_match_score >= 0.80:
            return 0.90
        if menu_match_score >= 0.70:
            return 0.82
    if not position_is_evidence:
        # Nothing identified this episode; only its length is known.
        return 0.35 if duration_seconds > 0 else 0.2
    if duration_seconds <= 0:
        return 0.45
    per_ep = duration_seconds / max(episode_count, 1) / 60.0
    if 6 <= per_ep <= 20:
        return 0.92 if episode_count == 1 else 0.84
    if 4 <= per_ep <= 35:
        return 0.75
    return 0.52


def _parse_raw_metadata(raw_json: Any) -> dict[str, Any]:
    if not raw_json:
        return {}
    if isinstance(raw_json, dict):
        return raw_json
    if isinstance(raw_json, str):
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_menu_name(raw: dict[str, Any]) -> str | None:
    value = raw.get("menu_name")
    if isinstance(value, str) and value.strip():
        return value.strip()
    mkvinfo = raw.get("makemkv_info")
    if isinstance(mkvinfo, dict):
        display = mkvinfo.get("display_name")
        if isinstance(display, str) and display.strip():
            return display.strip()
    return None


def _find_best_menu_match(menu_name: str, targets: list[EpisodeTarget]) -> dict[str, Any] | None:
    cleaned_menu = _normalize_name(menu_name)
    if not cleaned_menu:
        return None
    best = None
    for idx, ep in enumerate(targets):
        score = SequenceMatcher(None, cleaned_menu, _normalize_name(ep.title)).ratio()
        if best is None or score > best["score"]:
            best = {"index": idx, "score": score, "episode": ep}
    if best and best["score"] >= 0.72:
        return best
    return None


def _should_try_ocr_menu_fallback(menu_name: str | None) -> bool:
    if not menu_name:
        return True
    norm = _normalize_name(menu_name)
    if not norm:
        return True
    tokens = [t for t in norm.split(" ") if t]
    if not tokens:
        return True
    if re.fullmatch(r"(title|menu)?\s*[a-z]?\d{1,3}", norm):
        return True
    if re.fullmatch(r"b\d{1,3}", norm):
        return True
    if re.fullmatch(r"[a-z]\d{1,3}\s*t\d{1,3}", norm):
        return True
    if re.fullmatch(r"[a-z]\d{1,3}(?:\s+t\d{1,3})?(?:\s+ch(?:apter)?\d{1,3})?", norm):
        return True
    # Low-signal menu labels are often mostly numeric/technical tokens.
    alpha_tokens = [t for t in tokens if re.search(r"[a-z]", t)]
    meaningful_alpha = [
        t for t in alpha_tokens
        if len(t) >= 4 and t not in {"title", "menu", "chapter", "chapters", "track", "part"}
    ]
    if not meaningful_alpha:
        return True
    technical_hits = sum(
        1
        for t in tokens
        if re.fullmatch(r"(?:b|t)\d{1,3}", t)
        or re.fullmatch(r"\d+(?:\.\d+)?(?:gb|mb|min|sec|s)", t)
        or t in {"chapter", "chapters", "title", "menu"}
    )
    if technical_hits >= max(1, len(tokens) // 2):
        return True
    if len(norm) <= 5:
        return True
    return False


def _identify_likely_play_all_titles(rip_rows) -> set[int]:
    """
    Titles that replay the whole disc rather than holding one episode.

    The strongest evidence is arithmetic: a play-all runs the other episodes
    back to back, so its length is the sum of theirs. A 122-minute title on a
    disc of five 24-minute episodes is the play-all, whatever its filename.

    The old check additionally demanded an A-prefixed filename (a1_t05), but
    that letter is MakeMKV's source-group label and says nothing about
    play-all-ness -- an e1_t05 play-all sailed through, got treated as an
    episode, and swallowed a slack candidate. The filename rule survives only
    as a fallback for duration outliers whose sum does not line up.
    """
    durations = [float(r["duration_seconds"]) for r in rip_rows if r["duration_seconds"] is not None]
    if len(durations) < 2:
        return set()
    sorted_durations = sorted(durations)
    median = sorted_durations[len(sorted_durations) // 2]
    longest = max(durations)
    full_lengths = [d for d in durations if d >= 10 * 60]
    full_length_sum = sum(full_lengths)

    result: set[int] = set()
    for r in rip_rows:
        dur = float(r["duration_seconds"] or 0.0)
        if dur < 45 * 60:
            continue
        # The rest of the disc's full-length content, without this title. A
        # sum of one is not a sum: two 90-minute specials each equal the
        # other's "total", and flagging both would exclude the entire disc.
        is_full = dur >= 10 * 60
        others = full_length_sum - dur if is_full else full_length_sum
        others_count = len(full_lengths) - (1 if is_full else 0)
        if others_count >= 2 and others >= 20 * 60 and 0.85 <= (dur / others) <= 1.15:
            result.add(int(r["id"]))
            continue
        source_file = Path(str(r["source_file"] or ""))
        stem = source_file.stem.lower()
        if (
            dur >= max(longest * 0.90, median * 2.5, 45 * 60)
            and re.match(r"^a\d+_t\d+$", stem) is not None
        ):
            result.add(int(r["id"]))
    return result


def _find_best_ocr_menu_match(
    cfg: AppConfig,
    job_id: str,
    source_file: str,
    rip_title_id: int,
    targets: list[EpisodeTarget],
    disc_drive: str | None = None,
) -> dict[str, Any]:
    if not targets:
        return {"match": None, "artifact_image_path": None, "artifact_text_path": None, "best_score": None}
    ffmpeg = Path(cfg.ffmpeg_path)
    if not ffmpeg.exists():
        return {"match": None, "artifact_image_path": None, "artifact_text_path": None, "best_score": None}
    tesseract = _resolve_tesseract_executable()
    if tesseract is None:
        return {"match": None, "artifact_image_path": None, "artifact_text_path": None, "best_score": None}
    candidate_sources = _build_ocr_source_candidates(source_file, disc_drive)
    if not candidate_sources:
        return {
            "match": None,
            "artifact_image_path": None,
            "artifact_text_path": None,
            "best_score": None,
            "source_label": None,
        }

    ocr_root = Path(cfg.staging_root) / "jobs" / job_id / "ocr"
    artifacts_dir = ocr_root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    best: dict[str, Any] | None = None
    best_frame_bytes: bytes | None = None
    best_text = ""
    best_source_label: str | None = None
    best_source_tag: str | None = None
    best_source_kind: str | None = None

    dvdnav_frames = _capture_dvdnav_button_crops(cfg, job_id, generate=False)
    if dvdnav_frames:
        nav_match, nav_best_frame, nav_best_text = _match_episode_titles_from_images(
            tesseract_path=tesseract,
            image_files=dvdnav_frames,
            targets=targets,
        )
        if nav_match is not None:
            best = nav_match
            best_text = nav_best_text
            best_source_label = "libdvdnav button crops"
            best_source_tag = "dvdnav_buttons"
            best_source_kind = "menu"
            if nav_best_frame is not None:
                try:
                    best_frame_bytes = nav_best_frame.read_bytes()
                except OSError:
                    best_frame_bytes = None
        if best and float(best["score"]) >= 0.65:
            artifact_image_path = None
            artifact_text_path = None
            if best_frame_bytes is not None and best_source_tag:
                image_out = artifacts_dir / f"rip_title_{rip_title_id:04d}_{best_source_tag}_best.png"
                text_out = artifacts_dir / f"rip_title_{rip_title_id:04d}_{best_source_tag}_best.txt"
                image_out.write_bytes(best_frame_bytes)
                text_out.write_text(best_text, encoding="utf-8")
                artifact_image_path = str(image_out)
                artifact_text_path = str(text_out)
            return {
                "match": best,
                "artifact_image_path": artifact_image_path,
                "artifact_text_path": artifact_text_path,
                "best_score": float(best["score"]),
                "source_label": best_source_label,
                "source_kind": best_source_kind,
            }

    # Strongest pass: extract focused button crops using DVD-Archaeology's
    # nav/menu rectangles while skipping corrupt IFOs.
    dvd_arch_frames = _capture_dvd_archaeology_button_crops(cfg, job_id, generate=False)
    if dvd_arch_frames:
        arch_match, arch_best_frame, arch_best_text = _match_episode_titles_from_images(
            tesseract_path=tesseract,
            image_files=dvd_arch_frames,
            targets=targets,
        )
        if arch_match is not None:
            best = arch_match
            best_text = arch_best_text
            best_source_label = "DVD-Archaeology menu button crops"
            best_source_tag = "dvd_arch_buttons"
            best_source_kind = "menu"
            if arch_best_frame is not None:
                try:
                    best_frame_bytes = arch_best_frame.read_bytes()
                except OSError:
                    best_frame_bytes = None
        if best and float(best["score"]) >= 0.65:
            artifact_image_path = None
            artifact_text_path = None
            if best_frame_bytes is not None and best_source_tag:
                image_out = artifacts_dir / f"rip_title_{rip_title_id:04d}_{best_source_tag}_best.png"
                text_out = artifacts_dir / f"rip_title_{rip_title_id:04d}_{best_source_tag}_best.txt"
                image_out.write_bytes(best_frame_bytes)
                text_out.write_text(best_text, encoding="utf-8")
                artifact_image_path = str(image_out)
                artifact_text_path = str(text_out)
            return {
                "match": best,
                "artifact_image_path": artifact_image_path,
                "artifact_text_path": artifact_text_path,
                "best_score": float(best["score"]),
                "source_label": best_source_label,
                "source_kind": best_source_kind,
            }

    # First pass: nav-aware menu screenshots via VLC RC controls (best-effort).
    nav_frames = _capture_vlc_menu_snapshots(cfg, job_id, generate=False)
    if nav_frames:
        nav_match, nav_best_frame, nav_best_text = _match_episode_titles_from_images(
            tesseract_path=tesseract,
            image_files=nav_frames,
            targets=targets,
        )
        if nav_match is not None:
            best = nav_match
            best_text = nav_best_text
            best_source_label = "VLC nav menu snapshots"
            best_source_tag = "vlc_nav_menu"
            best_source_kind = "menu"
            if nav_best_frame is not None:
                try:
                    best_frame_bytes = nav_best_frame.read_bytes()
                except OSError:
                    best_frame_bytes = None
        # Keep this strict to avoid false-positive auto assignment from OCR noise.
        if best and float(best["score"]) >= 0.80:
            artifact_image_path = None
            artifact_text_path = None
            if best_frame_bytes is not None and best_source_tag:
                image_out = artifacts_dir / f"rip_title_{rip_title_id:04d}_{best_source_tag}_best.png"
                text_out = artifacts_dir / f"rip_title_{rip_title_id:04d}_{best_source_tag}_best.txt"
                image_out.write_bytes(best_frame_bytes)
                text_out.write_text(best_text, encoding="utf-8")
                artifact_image_path = str(image_out)
                artifact_text_path = str(text_out)
            return {
                "match": best,
                "artifact_image_path": artifact_image_path,
                "artifact_text_path": artifact_text_path,
                "best_score": float(best["score"]),
                "source_label": best_source_label,
                "source_kind": best_source_kind,
            }

    for source in candidate_sources:
        source_path = Path(source["path"])
        source_tag = str(source["tag"])
        source_label = str(source["label"])
        source_kind = str(source.get("kind") or "unknown")
        frame_dir = ocr_root / f"{source_tag}_frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame_pattern = frame_dir / "frame_%03d.png"
        try:
            # Prefer early timeline where menus are usually shown.
            extract_cmd = [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_path),
                "-t",
                "180",
                "-vf",
                "fps=1/4,scale=1280:-1,eq=contrast=1.2:brightness=0.05",
                str(frame_pattern),
            ]
            extract_proc = subprocess.run(
                extract_cmd,
                capture_output=True,
                timeout=300,
                check=False,
            )
            if extract_proc.returncode != 0:
                continue

            frame_files = sorted(frame_dir.glob("frame_*.png"))
            if len(frame_files) > 60:
                stride = max(1, len(frame_files) // 60)
                frame_files = frame_files[::stride][:60]
            for frame in frame_files:
                text = _ocr_frame_text(tesseract, frame)
                if not text:
                    continue
                match = _find_best_episode_title_in_text(text, targets)
                if match is None:
                    continue
                if best is None or float(match["score"]) > float(best["score"]):
                    best = match
                    try:
                        best_frame_bytes = frame.read_bytes()
                    except OSError:
                        best_frame_bytes = None
                    best_text = text
                    best_source_label = source_label
                    best_source_tag = source_tag
                    best_source_kind = source_kind
                if best and float(best["score"]) >= 0.93:
                    break
            if best and float(best["score"]) >= 0.93:
                break
        finally:
            shutil.rmtree(frame_dir, ignore_errors=True)

    artifact_image_path = None
    artifact_text_path = None
    if best_frame_bytes is not None and best_source_tag:
        image_out = artifacts_dir / (
            f"rip_title_{rip_title_id:04d}_{best_source_tag}_best.png"
        )
        text_out = artifacts_dir / (
            f"rip_title_{rip_title_id:04d}_{best_source_tag}_best.txt"
        )
        image_out.write_bytes(best_frame_bytes)
        text_out.write_text(best_text, encoding="utf-8")
        artifact_image_path = str(image_out)
        artifact_text_path = str(text_out)

    min_accept_score = 0.65 if best_source_kind == "menu" else 0.80
    if best and float(best["score"]) >= min_accept_score:
        return {
            "match": best,
            "artifact_image_path": artifact_image_path,
            "artifact_text_path": artifact_text_path,
            "best_score": float(best["score"]),
            "source_label": best_source_label,
            "source_kind": best_source_kind,
        }
    return {
        "match": None,
        "artifact_image_path": artifact_image_path,
        "artifact_text_path": artifact_text_path,
        "best_score": float(best["score"]) if best else None,
        "source_label": best_source_label,
        "source_kind": best_source_kind,
    }


def _build_ocr_source_candidates(source_file: str, expected_drive: str | None = None) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    menu_files = _discover_dvd_menu_vobs(expected_drive)
    for idx, menu_file in enumerate(menu_files, start=1):
        candidates.append(
            {
                "path": str(menu_file),
                "label": f"disc menu file {menu_file.name}",
                "tag": f"menu_{idx:02d}_{_sanitize_filename_token(menu_file.stem)}",
                "kind": "menu",
            }
        )
    # If menu sources are available, prefer those first for episode names.
    if candidates:
        return candidates
    src = Path(source_file)
    if src.exists():
        candidates.append(
            {
                "path": str(src),
                "label": f"ripped title file {src.name}",
                "tag": f"title_{_sanitize_filename_token(src.stem)}",
                "kind": "title",
            }
        )
    return candidates


def _discover_dvd_menu_vobs(expected_drive: str | None = None) -> list[Path]:
    """
    Menu VOBs on this job's disc.

    Without a drive to scope to, this returns nothing rather than searching
    every bay: OCR then falls back to the ripped title file, which is local and
    unambiguously this job's content. Preferring "whatever disc is loaded
    somewhere" is what had a Minnie's Pet Salon job reading the disc being
    ripped in the other drive.
    """
    if _job_disc_root(expected_drive) is None:
        return []
    files: list[Path] = []
    for drive in discover_optical_drives():
        if not drive.get("has_media"):
            continue
        letter = str(drive.get("drive") or "").strip().rstrip("\\").upper()
        if letter != str(expected_drive).strip().rstrip("\\").upper():
            continue
        root = Path(str(drive["root"]))
        video_ts = root / "VIDEO_TS"
        if not video_ts.exists():
            continue
        top_menu = video_ts / "VIDEO_TS.VOB"
        if top_menu.exists():
            files.append(top_menu)
        files.extend(sorted(video_ts.glob("VTS_*_0.VOB")))
    deduped: list[Path] = []
    seen: set[str] = set()
    for f in files:
        key = str(f).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    return deduped


def _capture_dvd_archaeology_menu_pages(cfg: AppConfig, job_id: str, generate: bool = False, disc_drive: str | None = None) -> list[Path]:
    disc_root = _job_disc_root(disc_drive)
    if disc_root is None:
        return []
    out_dir = Path(cfg.staging_root) / "jobs" / job_id / "dvd_arch_menu"
    out_dir.mkdir(parents=True, exist_ok=True)

    page_dir = out_dir / "menu_images" / "_menu_detect_multipage"
    page_files = sorted(page_dir.glob("*.png"))
    if page_files:
        return page_files

    detect_dir = out_dir / "menu_images" / "_menu_detect"
    detect_files = sorted(detect_dir.glob("*.png"))
    if detect_files:
        return detect_files

    if generate:
        _ensure_dvd_archaeology_artifacts(
            cfg=cfg,
            disc_root=disc_root,
            out_dir=out_dir,
            need_menu_images=True,
        )
    page_files = sorted(page_dir.glob("*.png"))
    if page_files:
        return page_files
    return sorted(detect_dir.glob("*.png"))


def _capture_dvdnav_button_crops(cfg: AppConfig, job_id: str, generate: bool = False, disc_drive: str | None = None) -> list[Path]:
    root = _job_disc_root(disc_drive)
    if root is None:
        return []
    drive_root = str(root)
    artifact_path = Path(cfg.staging_root) / "jobs" / job_id / "dvdnav_menu" / "dvdnav_menu.json"
    if generate or not artifact_path.exists():
        payload = extract_dvdnav_menu_artifacts(
            staging_root=cfg.staging_root,
            job_id=job_id,
            drive_root=drive_root,
        )
    else:
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    if not payload.get("available"):
        return []
    buttons = payload.get("buttons")
    if not isinstance(buttons, list) or not buttons:
        return []

    video_ts = Path(drive_root) / "VIDEO_TS"
    menu_vob = video_ts / "VIDEO_TS.VOB"
    if not menu_vob.exists():
        menu_candidates = sorted(video_ts.glob("VTS_*_0.VOB"))
        if not menu_candidates:
            return []
        menu_vob = menu_candidates[0]

    ffmpeg = Path(cfg.ffmpeg_path)
    if not ffmpeg.exists():
        return []

    out_dir = Path(cfg.staging_root) / "jobs" / job_id / "dvdnav_menu" / "button_crops"
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("*.png"))
    if existing and not generate:
        return existing
    for old in out_dir.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass
    if not generate:
        return []

    kept = 0
    sample_times = (0.1, 1.5, 3.0)
    seen: set[tuple[int, int, int, int]] = set()
    for button in buttons:
        rect = button.get("rect") if isinstance(button, dict) else None
        if not isinstance(rect, dict):
            continue
        x1 = int(rect.get("x1", 0))
        y1 = int(rect.get("y1", 0))
        x2 = int(rect.get("x2", 0))
        y2 = int(rect.get("y2", 0))
        if x2 <= x1 or y2 <= y1:
            continue
        key = (x1, y1, x2, y2)
        if key in seen:
            continue
        seen.add(key)
        w = x2 - x1 + 1
        h = y2 - y1 + 1
        button_id = int(button.get("button_id", 0) or 0)
        for idx, ts in enumerate(sample_times, start=1):
            out_file = out_dir / f"btn_{button_id:02d}_s{idx:02d}.png"
            cmd = [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{ts:.1f}",
                "-i",
                str(menu_vob),
                "-frames:v",
                "1",
                "-vf",
                f"crop={w}:{h}:{x1}:{y1},scale=1280:-1,eq=contrast=1.25:brightness=0.05",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=45,
                check=False,
            )
            if proc.returncode == 0 and out_file.exists():
                kept += 1
        if kept >= 60:
            break
    return sorted(out_dir.glob("*.png"))


def _capture_dvd_archaeology_button_crops(cfg: AppConfig, job_id: str, generate: bool = False, disc_drive: str | None = None) -> list[Path]:
    disc_root = _job_disc_root(disc_drive)
    if disc_root is None:
        return []
    out_dir = Path(cfg.staging_root) / "jobs" / job_id / "dvd_arch_menu"
    out_dir.mkdir(parents=True, exist_ok=True)

    menu_map_path = out_dir / "menu_map.json"
    if not menu_map_path.exists():
        if generate:
            _ensure_dvd_archaeology_artifacts(
                cfg=cfg,
                disc_root=disc_root,
                out_dir=out_dir,
                need_menu_images=False,
            )
    if not menu_map_path.exists():
        return _capture_dvd_archaeology_menu_pages(cfg, job_id, generate=generate)

    try:
        payload = json.loads(menu_map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _capture_dvd_archaeology_menu_pages(cfg, job_id, generate=generate)

    entries = payload.get("entries")
    if not isinstance(entries, list):
        return _capture_dvd_archaeology_menu_pages(cfg, job_id, generate=generate)

    crop_dir = out_dir / "ocr_button_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(crop_dir.glob("*.png"))
    if existing and not generate:
        return existing
    for old in crop_dir.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass
    if not generate:
        return []

    ffmpeg = Path(cfg.ffmpeg_path)
    if not ffmpeg.exists():
        return _capture_dvd_archaeology_menu_pages(cfg, job_id, generate=generate)

    seen: set[tuple[int, int, int, int, int, int, str]] = set()
    sample_times = (0.1, 3.1, 6.1, 9.1)
    kept = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rect = entry.get("selection_rect") or entry.get("rect")
        if not isinstance(rect, dict):
            continue
        x = int(rect.get("x", 0))
        y = int(rect.get("y", 0))
        w = int(rect.get("w", 0))
        h = int(rect.get("h", 0))
        menu_id = str(entry.get("menu_id") or "")
        if w <= 0 or h <= 0 or not menu_id:
            continue
        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        target_title = int(target.get("title_id") or 0)
        target_pgc = int(target.get("pgc_id") or 0)
        key = (x, y, w, h, target_title, target_pgc, menu_id)
        if key in seen:
            continue
        seen.add(key)
        menu_vob = _menu_vob_path_for_menu_id(disc_root, menu_id)
        if menu_vob is None or not menu_vob.exists():
            continue
        safe_menu = _sanitize_filename_token(menu_id)
        for idx, ts in enumerate(sample_times, start=1):
            out_file = crop_dir / (
                f"{safe_menu}_t{target_title:02d}_p{target_pgc:02d}_s{idx:02d}.png"
            )
            cmd = [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{ts:.1f}",
                "-i",
                str(menu_vob),
                "-frames:v",
                "1",
                "-vf",
                f"crop={w}:{h}:{x}:{y},scale=1280:-1,eq=contrast=1.25:brightness=0.05",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if proc.returncode == 0 and out_file.exists():
                kept += 1
        if kept >= 120:
            break

    crops = sorted(crop_dir.glob("*.png"))
    if crops:
        return crops
    return _capture_dvd_archaeology_menu_pages(cfg, job_id, generate=generate)


def _ensure_dvd_archaeology_artifacts(
    cfg: AppConfig,
    disc_root: Path,
    out_dir: Path,
    need_menu_images: bool,
) -> bool:
    menu_map_path = out_dir / "menu_map.json"
    if menu_map_path.exists() and not need_menu_images:
        return True
    detect_dir = out_dir / "menu_images" / "_menu_detect"
    multipage_dir = out_dir / "menu_images" / "_menu_detect_multipage"
    if need_menu_images and (
        any(detect_dir.glob("*.png")) or any(multipage_dir.glob("*.png"))
    ):
        return True

    for runner in _candidate_dvd_arch_python_commands():
        if _run_dvd_arch_menu_images(
            runner_cmd=runner,
            disc_root=disc_root,
            out_dir=out_dir,
            ffmpeg_dir=Path(cfg.ffmpeg_path).parent,
            until_stage="menu_images" if need_menu_images else "menu_map",
            timeout_seconds=240 if not need_menu_images else 420,
        ):
            return True
    return False


def _menu_vob_path_for_menu_id(disc_root: Path, menu_id: str) -> Path | None:
    video_ts = disc_root / "VIDEO_TS"
    if menu_id.upper().startswith("VMGM"):
        path = video_ts / "VIDEO_TS.VOB"
        return path if path.exists() else None
    match = re.match(r"VTSM_(\d{2})_", menu_id.upper())
    if not match:
        return None
    path = video_ts / f"VTS_{match.group(1)}_0.VOB"
    return path if path.exists() else None


def _candidate_dvd_arch_python_commands() -> list[list[str]]:
    commands: list[list[str]] = []
    py_launcher = shutil.which("py")
    if py_launcher:
        commands.append([py_launcher, "-3.11"])
    python311 = shutil.which("python3.11")
    if python311:
        commands.append([python311])
    # The Microsoft Store build of Python installs under the current
    # user's WindowsApps folder, which shutil.which often will not
    # surface. This used to be one absolute path with a specific username
    # baked into it: it matched on exactly one machine and silently did
    # nothing everywhere else.
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        store_apps = Path(local_app_data) / "Microsoft" / "WindowsApps"
        try:
            for entry in sorted(store_apps.glob("PythonSoftwareFoundation.Python.3.1*")):
                candidate = entry / "python.exe"
                if candidate.exists():
                    commands.append([str(candidate)])
        except OSError:
            pass
    commands.append([sys.executable])

    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for cmd in commands:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        unique.append(cmd)
    return unique


def _run_dvd_arch_menu_images(
    runner_cmd: list[str],
    disc_root: Path,
    out_dir: Path,
    ffmpeg_dir: Path,
    until_stage: str,
    timeout_seconds: int,
) -> bool:
    script = r"""
import os
import sys
from pathlib import Path

import pyparsedvd
import dvdmenu_extract.util.dvd_ifo as di
from dvdmenu_extract.pipeline import PipelineOptions, run_pipeline

orig_iter = di._iter_vts_ifo_files

def filtered_iter(video_ts: Path):
    for title_id, ifo_path in orig_iter(video_ts):
        try:
            with ifo_path.open("rb") as handle:
                pyparsedvd.load_vts_pgci(handle)
        except Exception:
            continue
        yield title_id, ifo_path

di._iter_vts_ifo_files = filtered_iter

opts = PipelineOptions(
    ocr_lang="eng",
    use_real_ocr=False,
    use_real_ffmpeg=True,
    repair="off",
    force=True,
    json_out_root=False,
    json_root_dir=False,
    use_real_timing=False,
    allow_dvd_ifo_fallback=True,
    debug_spu=False,
)

try:
    run_pipeline(Path(sys.argv[1]), Path(sys.argv[2]), opts, until=sys.argv[3])
except Exception:
    pass
"""
    env = os.environ.copy()
    env["PATH"] = str(ffmpeg_dir) + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.Popen(
            runner_cmd + ["-c", script, str(disc_root), str(out_dir), until_stage],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        return False
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return False
    page_dir = out_dir / "menu_images" / "_menu_detect_multipage"
    detect_dir = out_dir / "menu_images" / "_menu_detect"
    if until_stage == "menu_map":
        return (out_dir / "menu_map.json").exists()
    return any(page_dir.glob("*.png")) or any(detect_dir.glob("*.png"))


def _capture_vlc_menu_snapshots(cfg: AppConfig, job_id: str, generate: bool = False, disc_drive: str | None = None) -> list[Path]:
    vlc_path = _resolve_vlc_executable()
    if vlc_path is None:
        return []
    if _job_disc_root(disc_drive) is None:
        return []
    drive = str(disc_drive)

    snapshot_dir = Path(cfg.staging_root) / "jobs" / job_id / "ocr" / "vlc_nav"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    if not generate:
        return sorted(snapshot_dir.glob("nav_*.png"))

    # Clear old snapshots so each mapping pass has current artifacts.
    for old in snapshot_dir.glob("nav_*.png"):
        try:
            old.unlink()
        except OSError:
            pass

    cmd = [
        str(vlc_path),
        f"dvd:///{drive}",
        "--intf",
        "qt",
        "--no-video-title-show",
        "--video-on-top",
        "--snapshot-path",
        str(snapshot_dir),
        "--snapshot-prefix",
        "nav",
        "--snapshot-format",
        "png",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return []

    try:
        time.sleep(7.0)
        if not _focus_vlc_window():
            return []
        # Visible-window automation: snapshot hotkey is Shift+S.
        sequence = [
            "+s",
            "{RIGHT}", "+s",
            "{RIGHT}", "+s",
            "{DOWN}", "+s",
            "{LEFT}", "+s",
            "{UP}", "+s",
            "{ENTER}", "+s",
        ]
        for step in sequence:
            if not _send_vlc_keys(step):
                break
            time.sleep(1.2)
    finally:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    return sorted(snapshot_dir.glob("nav_*.png"))


def _resolve_vlc_executable() -> Path | None:
    env_path = shutil.which("vlc")
    if env_path:
        return Path(env_path)
    common_paths = (
        Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
        Path(r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
    )
    for candidate in common_paths:
        if candidate.exists():
            return candidate
    return None


def _focus_vlc_window() -> bool:
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "if ($ws.AppActivate('VLC media player')) { exit 0 } else { exit 1 }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return proc.returncode == 0


def _send_vlc_keys(keys: str) -> bool:
    escaped = keys.replace("'", "''")
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "if (-not $ws.AppActivate('VLC media player')) { exit 1 }; "
        f"$ws.SendKeys('{escaped}'); "
        "exit 0"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return proc.returncode == 0


def _match_episode_titles_from_images(
    tesseract_path: Path,
    image_files: list[Path],
    targets: list[EpisodeTarget],
) -> tuple[dict[str, Any] | None, Path | None, str]:
    best: dict[str, Any] | None = None
    best_frame: Path | None = None
    best_text = ""
    for image_file in image_files:
        text = _ocr_frame_text(tesseract_path, image_file)
        if not text:
            continue
        match = _find_best_episode_title_in_text(text, targets)
        if match is None:
            continue
        if best is None or float(match["score"]) > float(best["score"]):
            best = match
            best_frame = image_file
            best_text = text
    return best, best_frame, best_text


def _sanitize_filename_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_")
    return token or "unknown"


def _resolve_tesseract_executable() -> Path | None:
    env_path = shutil.which("tesseract")
    if env_path:
        return Path(env_path)
    common_paths = (
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    )
    for candidate in common_paths:
        if candidate.exists():
            return candidate
    return None


def _ocr_frame_text(tesseract_path: Path, frame_file: Path) -> str:
    outputs: list[str] = []
    for psm in ("11", "6"):
        cmd = [
            str(tesseract_path),
            str(frame_file),
            "stdout",
            "--oem",
            "1",
            "--psm",
            psm,
            "-l",
            "eng",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            continue
        outputs.append(proc.stdout.decode("utf-8", errors="replace"))
    return "\n".join(outputs)


def _find_best_episode_title_in_text(
    ocr_text: str,
    targets: list[EpisodeTarget],
) -> dict[str, Any] | None:
    matches = _find_episode_title_matches_in_text(ocr_text, targets)
    if not matches:
        return None
    multi = _best_contiguous_multi_match(matches)
    if multi is not None:
        return multi

    best: dict[str, Any] | None = None
    for match in matches:
        if best is None or float(match["score"]) > float(best["score"]):
            best = match
    return best


def _find_episode_title_matches_in_text(
    ocr_text: str,
    targets: list[EpisodeTarget],
) -> list[dict[str, Any]]:
    raw_chunks: list[str] = []
    for line in ocr_text.splitlines():
        raw_chunks.append(line)
        raw_chunks.extend(part for part in re.split(r"[|/\\]+", line) if part.strip())
    full_text = _normalize_name(" ".join(raw_chunks))
    chunks = [_normalize_name(c) for c in raw_chunks]
    chunks = [c for c in chunks if len(c) >= 4]
    if not chunks and not full_text:
        return []

    best_by_index: dict[int, dict[str, Any]] = {}
    for idx, ep in enumerate(targets):
        episode_name = _normalize_name(ep.title)
        if not episode_name:
            continue
        best_score = 0.0
        best_position = 10_000
        iter_chunks = chunks or ([full_text] if full_text else [])
        for chunk_pos, chunk in enumerate(iter_chunks):
            if episode_name in chunk:
                score = 0.99
            elif chunk in episode_name and len(chunk) >= max(8, len(episode_name) // 2):
                score = 0.90
            else:
                seq_score = SequenceMatcher(None, chunk, episode_name).ratio()
                chunk_tokens = set(chunk.split())
                ep_tokens = set(episode_name.split())
                overlap_score = (
                    len(chunk_tokens & ep_tokens) / max(1, len(ep_tokens))
                    if chunk_tokens and ep_tokens
                    else 0.0
                )
                score = max(seq_score, (0.80 * overlap_score) + (0.20 * seq_score))
            if score > best_score:
                best_score = score
                best_position = chunk_pos
        if full_text:
            token_overlap = _episode_token_overlap_score(full_text, episode_name)
            if token_overlap >= 0.60:
                score = max(0.78, token_overlap)
                if score > best_score:
                    best_score = score
                    best_position = min(best_position, 9999)
        if best_score <= 0.0:
            continue
        if best_score >= 0.72:
            best_by_index[idx] = {
                "index": idx,
                "score": best_score,
                "episode": ep,
                "position": best_position,
            }
    return sorted(best_by_index.values(), key=lambda m: (int(m["position"]), int(m["index"])))


def _best_contiguous_multi_match(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(matches) < 2:
        return None
    best_run: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = [matches[0]]
    for match in matches[1:]:
        prev = current[-1]
        if int(match["index"]) == int(prev["index"]) + 1:
            current.append(match)
        else:
            if len(current) > len(best_run):
                best_run = current[:]
            current = [match]
    if len(current) > len(best_run):
        best_run = current[:]
    if len(best_run) < 2:
        return None
    avg_score = sum(float(m["score"]) for m in best_run) / len(best_run)
    if avg_score < 0.76:
        return None
    return {
        "index": int(best_run[0]["index"]),
        "indices": [int(m["index"]) for m in best_run],
        "score": avg_score,
        "episode": best_run[0]["episode"],
    }


def _episode_token_overlap_score(text_value: str, episode_name: str) -> float:
    text_tokens = [t for t in text_value.split() if len(t) >= 3]
    ep_tokens = [t for t in episode_name.split() if len(t) >= 3]
    if not text_tokens or not ep_tokens:
        return 0.0
    overlap = sum(1 for t in ep_tokens if any(t == x or t in x or x in t for x in text_tokens))
    return overlap / max(1, len(ep_tokens))


def _normalize_name(value: str) -> str:
    v = value.lower()
    v = re.sub(r"[_\-\.]+", " ", v)
    v = re.sub(r"[^a-z0-9 ]+", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    return v

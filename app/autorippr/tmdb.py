import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .config import AppConfig
from .logger import get_logger
from .state import append_job_log


log = get_logger("tmdb")

TMDB_BASE_URL = "https://api.themoviedb.org/3"


class TmdbError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscHints:
    normalized_query: str
    detected_season: int | None
    detected_year: int | None


def identify_job_with_tmdb(conn, cfg: AppConfig, job_id: str) -> dict[str, Any]:
    job = conn.execute(
        """
        SELECT id, disc_label, media_type, movie_mode, status, season_number, disc_scope, episode_range_start, episode_range_end
        FROM jobs WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    if not job:
        raise TmdbError(f"Job not found: {job_id}")

    disc_label = str(job["disc_label"] or "")
    media_type = str(job["media_type"] or "tv")
    hints = parse_disc_hints(disc_label)
    job_season_number = int(job["season_number"]) if job["season_number"] is not None else None
    season_hint = job_season_number if job_season_number is not None else hints.detected_season

    # duration hints from rip titles (if available)
    duration_rows = conn.execute(
        "SELECT duration_seconds FROM rip_titles WHERE job_id = ?",
        (job_id,),
    ).fetchall()
    durations = [
        float(r["duration_seconds"])
        for r in duration_rows
        if r["duration_seconds"] is not None
    ]
    avg_runtime_minutes = (sum(durations) / len(durations) / 60.0) if durations else None

    primary_query = _normalize_identify_query(disc_label, media_type)
    query_variants = _build_identify_queries(conn, cfg, job_id, primary_query or hints.normalized_query, media_type)
    ranked = _rank_candidates_for_query_variants(
        conn=conn,
        cfg=cfg,
        media_type=media_type,
        query_variants=query_variants,
        hint_year=hints.detected_year,
        hint_season=season_hint,
        avg_runtime_minutes=avg_runtime_minutes,
    )
    _persist_tmdb_candidates(conn, job_id, ranked)

    threshold = float(cfg.tmdb_confidence_threshold)
    movie_mode = str(job["movie_mode"] or "single")
    required_movie_slots = _required_movie_slots(movie_mode)
    selected_movie_slots = list_selected_movie_slots(conn, job_id) if media_type == "movie" and movie_mode != "single" else []
    same_type_ranked = [
        candidate for candidate in ranked
        if str(candidate.get("media_type") or "") == media_type
    ]
    selected = same_type_ranked[0] if same_type_ranked else None
    auto_selected_by_heuristic = False
    if media_type == "movie" and movie_mode != "single":
        needs_review = len(selected_movie_slots) < required_movie_slots
        selected = None
    else:
        needs_review = not selected or selected["score"] < threshold
        if needs_review and selected:
            auto_selected_by_heuristic = _should_auto_select_primary_candidate(
                ranked=same_type_ranked,
                requested_media_type=media_type,
                threshold=threshold,
            )
            if auto_selected_by_heuristic:
                needs_review = False

    if media_type == "movie" and movie_mode != "single" and not needs_review:
        append_job_log(
            conn,
            job_id,
            "INFO",
            f"Movie pack selection complete: {len(selected_movie_slots)}/{required_movie_slots} selected",
            None,
            None,
        )
    elif selected and not needs_review:
        _mark_selected_candidate(conn, job_id, selected["tmdb_id"], selected["media_type"])
        _upsert_selected_media(
            conn=conn,
            job_id=job_id,
            media_type=selected["media_type"],
            tmdb_id=selected["tmdb_id"],
            title=selected["title"],
            year=selected.get("year"),
            season_number=season_hint,
            order_mode=cfg.default_order_mode,
        )
        if auto_selected_by_heuristic:
            reason = (
                f"Auto-selected TMDB {selected['media_type']} {selected['tmdb_id']} "
                f"using single-primary heuristic score={selected['score']:.3f} threshold={threshold:.3f}"
            )
        else:
            reason = (
                f"Auto-selected TMDB {selected['media_type']} {selected['tmdb_id']} "
                f"score={selected['score']:.3f} >= threshold={threshold:.3f}"
            )
        append_job_log(conn, job_id, "INFO", reason, None, None)
    else:
        if media_type == "movie" and movie_mode != "single":
            reason = (
                f"Movie pack selection required: {len(selected_movie_slots)}/{required_movie_slots} selected"
            )
        else:
            reason = (
                "TMDB candidates need manual review"
                if selected
                else "TMDB search returned no candidates; manual review required"
            )
        append_job_log(conn, job_id, "WARNING", reason, None, None)

    conn.commit()
    return {
        "job_id": job_id,
        "query": hints.normalized_query,
        "query_variants": query_variants,
        "detected_season": season_hint,
        "detected_year": hints.detected_year,
        "avg_runtime_minutes": avg_runtime_minutes,
        "disc_scope": job["disc_scope"],
        "episode_range_start": job["episode_range_start"],
        "episode_range_end": job["episode_range_end"],
        "needs_review": needs_review,
        "confidence_threshold": threshold,
        "selected": selected if selected and not needs_review else None,
        "movie_mode": movie_mode,
        "required_movie_slots": required_movie_slots,
        "selected_movie_slots": selected_movie_slots,
        "top_candidates": ranked[:10],
    }


def search_job_with_tmdb_query(conn, cfg: AppConfig, job_id: str, query: str) -> dict[str, Any]:
    job = conn.execute(
        """
        SELECT id, disc_label, media_type, movie_mode, status, season_number
        FROM jobs WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    if not job:
        raise TmdbError(f"Job not found: {job_id}")

    media_type = str(job["media_type"] or "tv")
    job_season_number = int(job["season_number"]) if job["season_number"] is not None else None
    duration_rows = conn.execute(
        "SELECT duration_seconds FROM rip_titles WHERE job_id = ?",
        (job_id,),
    ).fetchall()
    durations = [
        float(r["duration_seconds"])
        for r in duration_rows
        if r["duration_seconds"] is not None
    ]
    avg_runtime_minutes = (sum(durations) / len(durations) / 60.0) if durations else None

    normalized_query = _normalize_identify_query(query, media_type)
    query_variants = [{"query": normalized_query, "source": "manual_query"}]
    if media_type == "movie":
        for variant in _movie_query_variants(normalized_query):
            if variant != normalized_query:
                query_variants.append({"query": variant, "source": "manual_query_variant"})
        debranded = _strip_movie_brand_tokens(normalized_query)
        if debranded and debranded != normalized_query:
            query_variants.append({"query": debranded, "source": "manual_query_debranded"})
            for variant in _movie_query_variants(debranded):
                if variant != debranded:
                    query_variants.append({"query": variant, "source": "manual_query_debranded_variant"})

    ranked = _rank_candidates_for_query_variants(
        conn=conn,
        cfg=cfg,
        media_type=media_type,
        query_variants=query_variants,
        hint_year=_extract_year(query),
        hint_season=job_season_number,
        avg_runtime_minutes=avg_runtime_minutes,
    )
    _persist_tmdb_candidates(conn, job_id, ranked)
    append_job_log(conn, job_id, "INFO", f"Manual TMDB search executed: query={normalized_query}", None, None)
    conn.commit()
    return {
        "job_id": job_id,
        "query": normalized_query,
        "top_candidates": ranked[:10],
    }


def select_tmdb_candidate(conn, job_id: str, tmdb_id: int, media_type: str, slot_index: int | None = None) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, job_id, tmdb_id, media_type, title, year, score
        FROM tmdb_candidates
        WHERE job_id = ? AND tmdb_id = ? AND media_type = ?
        LIMIT 1
        """,
        (job_id, tmdb_id, media_type),
    ).fetchone()
    if not row:
        raise TmdbError(
            f"Candidate not found for job={job_id}, media_type={media_type}, tmdb_id={tmdb_id}"
        )

    job = conn.execute(
        "SELECT season_number, movie_mode FROM jobs WHERE id = ? LIMIT 1",
        (job_id,),
    ).fetchone()
    job_season_number = int(job["season_number"]) if job and job["season_number"] is not None else None

    movie_mode = str(job["movie_mode"] or "single") if job else "single"
    if media_type == "movie" and movie_mode != "single":
        required_slots = _required_movie_slots(movie_mode)
        if slot_index is None or slot_index < 1 or slot_index > required_slots:
            raise TmdbError(f"Movie slot index must be between 1 and {required_slots}.")
        _upsert_selected_movie_slot(
            conn=conn,
            job_id=job_id,
            slot_index=slot_index,
            tmdb_id=int(row["tmdb_id"]),
            title=str(row["title"]),
            year=row["year"],
        )
        append_job_log(
            conn,
            job_id,
            "INFO",
            f"Manual movie slot selection applied: slot={slot_index} movie {tmdb_id}",
            None,
            None,
        )
        conn.commit()
        return dict(row)

    conn.execute(
        "UPDATE tmdb_candidates SET selected = 0, manual_override = 0 WHERE job_id = ?",
        (job_id,),
    )
    conn.execute(
        """
        UPDATE tmdb_candidates
        SET selected = 1, manual_override = 1
        WHERE job_id = ? AND tmdb_id = ? AND media_type = ?
        """,
        (job_id, tmdb_id, media_type),
    )
    _upsert_selected_media(
        conn=conn,
        job_id=job_id,
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=str(row["title"]),
        year=row["year"],
        season_number=job_season_number,
        order_mode=None,
    )
    append_job_log(
        conn,
        job_id,
        "INFO",
        f"Manual TMDB selection applied: {media_type} {tmdb_id}",
        None,
        None,
    )
    conn.commit()
    return dict(row)


def parse_disc_hints(disc_label: str) -> DiscHints:
    text = (disc_label or "").strip()
    normalized = _normalize_query(text)
    season = _extract_season(text)
    year = _extract_year(text)
    return DiscHints(
        normalized_query=normalized,
        detected_season=season,
        detected_year=year,
    )


def _build_identify_queries(conn, cfg: AppConfig, job_id: str, normalized_query: str, media_type: str) -> list[dict[str, str]]:
    queries: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(query: str, source: str) -> None:
        normalized = _normalize_identify_query(query, media_type)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        queries.append({"query": normalized, "source": source})

    add(normalized_query, "disc_label")
    if media_type == "movie":
        yearless = _strip_terminal_year_token(normalized_query)
        if yearless != normalized_query:
            add(yearless, "disc_label_yearless")
        for variant in _movie_query_variants(normalized_query):
            if variant != normalized_query:
                add(variant, "disc_label_variant")
        for variant in _movie_query_variants(yearless):
            if variant != yearless:
                add(variant, "disc_label_yearless_variant")
        debranded = _strip_movie_brand_tokens(normalized_query)
        if debranded != normalized_query:
            add(debranded, "disc_label_debranded")
            for variant in _movie_query_variants(debranded):
                if variant != debranded:
                    add(variant, "disc_label_debranded_variant")
        for hint in _collect_movie_title_hints(conn, cfg, job_id):
            add(str(hint["text"]), str(hint["source"]))
            if len(queries) >= 3:
                break
    return queries or [{"query": normalized_query, "source": "disc_label"}]


def _collect_movie_title_hints(conn, cfg: AppConfig, job_id: str) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT duration_seconds, source_file, raw_metadata_json
        FROM rip_titles
        WHERE job_id = ?
        ORDER BY duration_seconds DESC, id ASC
        """,
        (job_id,),
    ).fetchall()
    hints: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_hint(candidate: str, source: str) -> bool:
        normalized = _normalize_identify_query(candidate, "movie")
        if not _is_signal_query(normalized) or normalized in seen:
            return False
        seen.add(normalized)
        hints.append({"text": candidate, "source": source})
        return True

    for row in rows:
        raw = _parse_raw_metadata_json(row["raw_metadata_json"])
        candidates = [
            raw.get("menu_name"),
            (raw.get("makemkv_info") or {}).get("display_name") if isinstance(raw.get("makemkv_info"), dict) else None,
        ]
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            if add_hint(candidate, "rip_title_label"):
                break
    analysis_path = Path(cfg.staging_root) / "jobs" / job_id / "menu_analysis" / "menu_analysis.json"
    if analysis_path.exists():
        try:
            payload = json.loads(analysis_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        raw_hints = payload.get("media_title_hints") if isinstance(payload, dict) else None
        if isinstance(raw_hints, list):
            for hint in raw_hints:
                if not isinstance(hint, dict):
                    continue
                text = hint.get("text")
                source = hint.get("source") or "menu_analysis_ocr"
                if not isinstance(text, str):
                    continue
                add_hint(text, str(source))
    for row in rows:
        source_stem = Path(str(row["source_file"] or "")).stem
        add_hint(source_stem, "source_file_stem")
    return hints


def _parse_raw_metadata_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_signal_query(query: str) -> bool:
    if not query:
        return False
    if len(query) < 5:
        return False
    tokens = [token for token in query.split(" ") if token]
    if not tokens:
        return False
    alpha_tokens = [token for token in tokens if re.search(r"[a-z]", token)]
    if not alpha_tokens:
        return False
    technical_tokens = {
        "disc",
        "disk",
        "dvd",
        "title",
        "menu",
        "chapter",
        "chapters",
        "track",
        "part",
    }
    meaningful = [token for token in alpha_tokens if token not in technical_tokens and len(token) >= 3]
    return bool(meaningful)


def _normalize_identify_query(text: str, media_type: str) -> str:
    return _normalize_query(text, preserve_numbers=(media_type == "movie"))


def _strip_movie_brand_tokens(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(
        r"\b(disney|pixar|dreamworks|dreamworks_animation|illumination|sony|marvel|dc|warner|universal)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _movie_query_variants(text: str) -> list[str]:
    variants: list[str] = []
    if " and " in text:
        variants.append(text.replace(" and ", " & "))
    if " & " in text:
        variants.append(text.replace(" & ", " and "))
    return variants


def _strip_terminal_year_token(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"\b(19\d{2}|20\d{2})\b$", "", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or text


def _normalize_query(text: str, preserve_numbers: bool = False) -> str:
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[_\-\.]+", " ", t)
    t = re.sub(r"[<>\[\]\(\)\{\}\"'`]+", " ", t)
    # strip common disc tokens
    t = re.sub(r"\b(disc|disk|dvd|vol|volume|season|ep|episode)\b", " ", t)
    if not preserve_numbers:
        t = re.sub(r"\b\d{1,2}\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_season(text: str) -> int | None:
    if not text:
        return None
    patterns = [
        r"(?i)\bseason\s*(\d{1,2})\b",
        r"(?i)\bs(\d{1,2})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return int(m.group(1))
    return None


def _extract_year(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return int(m.group(1)) if m else None


def _search_and_score_candidates(
    conn,
    cfg: AppConfig,
    query: str,
    media_type: str,
    hint_year: int | None,
    hint_season: int | None,
    avg_runtime_minutes: float | None,
    limit: int = 10,
    query_source: str = "disc_label",
) -> list[dict[str, Any]]:
    if not query:
        return []

    endpoint = f"/search/{media_type}"
    params = {"query": query, "include_adult": "false", "page": 1}
    payload = _cached_tmdb_get(conn, cfg, endpoint, params)
    raw_results = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(raw_results, list):
        return []

    candidates: list[dict[str, Any]] = []
    for item in raw_results[:limit]:
        candidate = _build_candidate(
            media_type=media_type,
            query=query,
            query_source=query_source,
            item=item,
            hint_year=hint_year,
            hint_season=hint_season,
            avg_runtime_minutes=avg_runtime_minutes,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _rank_candidates_for_query_variants(
    conn,
    cfg: AppConfig,
    media_type: str,
    query_variants: list[dict[str, str]],
    hint_year: int | None,
    hint_season: int | None,
    avg_runtime_minutes: float | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if media_type in ("tv", "movie"):
        primary = media_type
        secondary = "movie" if media_type == "tv" else "tv"
        for query_variant in query_variants:
            candidates.extend(
                _search_and_score_candidates(
                    conn=conn,
                    cfg=cfg,
                    query=query_variant["query"],
                    media_type=primary,
                    hint_year=hint_year,
                    hint_season=hint_season,
                    avg_runtime_minutes=avg_runtime_minutes,
                    query_source=str(query_variant["source"]),
                )
            )
        if len(candidates) < 3 or (candidates and candidates[0]["score"] < 0.60):
            for query_variant in query_variants:
                candidates.extend(
                    _search_and_score_candidates(
                        conn=conn,
                        cfg=cfg,
                        query=query_variant["query"],
                        media_type=secondary,
                        hint_year=hint_year,
                        hint_season=hint_season,
                        avg_runtime_minutes=avg_runtime_minutes,
                        limit=5,
                        query_source=str(query_variant["source"]),
                    )
                )
    else:
        for query_variant in query_variants:
            candidates.extend(
                _search_and_score_candidates(
                    conn=conn,
                    cfg=cfg,
                    query=query_variant["query"],
                    media_type="tv",
                    hint_year=hint_year,
                    hint_season=hint_season,
                    avg_runtime_minutes=avg_runtime_minutes,
                    query_source=str(query_variant["source"]),
                )
            )
            candidates.extend(
                _search_and_score_candidates(
                    conn=conn,
                    cfg=cfg,
                    query=query_variant["query"],
                    media_type="movie",
                    hint_year=hint_year,
                    hint_season=hint_season,
                    avg_runtime_minutes=avg_runtime_minutes,
                    limit=5,
                    query_source=str(query_variant["source"]),
                )
            )

    deduped = _dedupe_candidates(candidates)
    ranked = sorted(deduped, key=lambda x: x["score"], reverse=True)
    return _rescore_with_actual_runtimes(conn, cfg, ranked, avg_runtime_minutes)


# How many of the leading candidates get a runtime lookup. Each one is a
# detail request, cached in tmdb_cache after the first time. Same-title films
# cluster at the top, so the tie that needs breaking is always in this window.
RUNTIME_LOOKUP_LIMIT = 6

# Extra lookups allowed for rivals that share the leader's exact title, which
# are the only ones the ambiguity guard actually weighs.
SAME_TITLE_LOOKUP_LIMIT = 12


def _rescore_with_actual_runtimes(
    conn,
    cfg: AppConfig,
    ranked: list[dict[str, Any]],
    avg_runtime_minutes: float | None,
) -> list[dict[str, Any]]:
    """
    Break ties using the runtime of the disc we actually ripped.

    Search results carry no runtime, so scoring could only apply a coarse prior
    based on the ripped duration alone -- it never compared it to anything. The
    result was that same-title films ("Robin Hood", "Pride and Prejudice",
    "Overboard") were indistinguishable and fell through to manual review, even
    though the ripped runtime identifies them almost uniquely.

    The detail endpoint has the runtime, and responses are cached, so this
    costs a handful of requests once per disc.
    """
    if not avg_runtime_minutes or avg_runtime_minutes <= 0 or not ranked:
        return ranked

    for candidate in ranked[:RUNTIME_LOOKUP_LIMIT]:
        _resolve_candidate_runtime(conn, cfg, candidate, avg_runtime_minutes)

    ranked = sorted(ranked, key=lambda x: x["score"], reverse=True)

    # Every rival sharing the leader's title also needs a runtime, even if it
    # sits outside the window above. The ambiguity guard treats an unknown
    # runtime as unresolved, so leaving these blank would make a title like
    # "Robin Hood" permanently ambiguous no matter how exact the match.
    leader_title = _normalize_identify_query(str(ranked[0].get("title") or ""), "movie")
    unchecked = [
        candidate
        for candidate in ranked
        if _shares_title(candidate, leader_title) and _runtime_lookup_state(candidate) is None
    ]
    for candidate in unchecked[:SAME_TITLE_LOOKUP_LIMIT]:
        _resolve_candidate_runtime(conn, cfg, candidate, avg_runtime_minutes)
    if unchecked:
        ranked = sorted(ranked, key=lambda x: x["score"], reverse=True)

    return ranked


def _shares_title(candidate: dict[str, Any], title: str) -> bool:
    if not title:
        return False
    return _normalize_identify_query(str(candidate.get("title") or ""), "movie") == title


def _runtime_lookup_state(candidate: dict[str, Any]) -> str | None:
    """"resolved", "unavailable", or None when no lookup was attempted."""
    breakdown = candidate.get("score_breakdown")
    if not isinstance(breakdown, dict):
        return None
    value = breakdown.get("runtime_lookup")
    return str(value) if value else None


def _resolve_candidate_runtime(
    conn,
    cfg: AppConfig,
    candidate: dict[str, Any],
    avg_runtime_minutes: float,
) -> None:
    """
    Look a candidate's runtime up and record the outcome.

    Recording *that we asked* matters as much as the answer: TMDB simply has no
    runtime for obscure or unreleased entries, and those must not veto an
    otherwise decisive match the way a genuinely unchecked rival would.
    """
    breakdown = candidate.get("score_breakdown")
    if not isinstance(breakdown, dict):
        return
    runtime = _fetch_candidate_runtime(conn, cfg, candidate)
    if runtime is None:
        breakdown["runtime_lookup"] = "unavailable"
        return
    breakdown["runtime_lookup"] = "resolved"
    _apply_runtime_to_candidate(candidate, avg_runtime_minutes, runtime)


def _fetch_candidate_runtime(conn, cfg: AppConfig, candidate: dict[str, Any]) -> float | None:
    """Runtime in minutes from the detail endpoint, or None if unavailable."""
    media_type = str(candidate.get("media_type") or "")
    tmdb_id = candidate.get("tmdb_id")
    if not isinstance(tmdb_id, int) or media_type not in ("movie", "tv"):
        return None
    try:
        detail = _cached_tmdb_get(conn, cfg, f"/{media_type}/{tmdb_id}", {})
    except TmdbError:
        # A detail lookup failing must never sink identification; the
        # candidate simply keeps its prior score.
        return None
    if not isinstance(detail, dict):
        return None

    if media_type == "movie":
        runtime = detail.get("runtime")
    else:
        # TV reports a list of typical episode runtimes.
        episode_runtimes = detail.get("episode_run_time")
        runtime = episode_runtimes[0] if isinstance(episode_runtimes, list) and episode_runtimes else None

    try:
        value = float(runtime)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _apply_runtime_to_candidate(
    candidate: dict[str, Any],
    ripped_minutes: float,
    candidate_minutes: float,
) -> None:
    """Swap the runtime prior for a real comparison and recompute the score."""
    breakdown = candidate.get("score_breakdown")
    if not isinstance(breakdown, dict):
        return
    weights = breakdown.get("weights")
    if not isinstance(weights, dict):
        return

    previous = float(breakdown.get("runtime_fit") or 0.0)
    actual = _score_runtime_match(ripped_minutes, candidate_minutes)
    weight = float(weights.get("runtime_fit") or 0.0)

    candidate["score"] = round(float(candidate.get("score") or 0.0) + (actual - previous) * weight, 6)
    breakdown["runtime_fit"] = round(actual, 6)
    breakdown["candidate_runtime_minutes"] = candidate_minutes
    breakdown["runtime_delta_minutes"] = round(abs(ripped_minutes - candidate_minutes), 2)


def _score_runtime_match(ripped_minutes: float, candidate_minutes: float) -> float:
    """
    How well a candidate's runtime matches the disc, in [0, 1].

    Tolerances are wide enough to absorb the usual discrepancies -- PAL speedup
    is ~4%, and TMDB runtimes are often rounded or taken from a different cut
    -- while still separating films that merely share a title.
    """
    if ripped_minutes <= 0 or candidate_minutes <= 0:
        return 0.5
    delta = abs(ripped_minutes - candidate_minutes)
    relative = delta / candidate_minutes

    if delta <= 2 or relative <= 0.02:
        return 1.0
    if delta <= 5 or relative <= 0.05:
        return 0.85
    if delta <= 10 or relative <= 0.10:
        return 0.6
    if delta <= 20:
        return 0.3
    return 0.0


def _build_candidate(
    media_type: str,
    query: str,
    query_source: str,
    item: dict[str, Any],
    hint_year: int | None,
    hint_season: int | None,
    avg_runtime_minutes: float | None,
) -> dict[str, Any] | None:
    tmdb_id = item.get("id")
    if not isinstance(tmdb_id, int):
        return None

    title = str(item.get("name") or item.get("title") or "").strip()
    if not title:
        return None

    year = _extract_candidate_year(item)
    tmdb_popularity = float(item.get("popularity") or 0.0)
    vote_count = int(item.get("vote_count") or 0)
    first_air_date = item.get("first_air_date")
    release_date = item.get("release_date")

    title_similarity = _string_similarity(query, title)
    year_score = _score_year(hint_year, year)
    season_score = _score_season_hint(hint_season, title, item)
    runtime_score = _score_runtime_hint(avg_runtime_minutes, item)
    popularity_score = min(tmdb_popularity / 50.0, 1.0)
    vote_score = min(vote_count / 5000.0, 1.0)

    weights = {
        "title_similarity": 0.52,
        "year_proximity": 0.18,
        "runtime_fit": 0.12,
        "season_hint": 0.10,
        "popularity": 0.05,
        "votes": 0.03,
    }
    score = (
        title_similarity * weights["title_similarity"]
        + year_score * weights["year_proximity"]
        + runtime_score * weights["runtime_fit"]
        + season_score * weights["season_hint"]
        + popularity_score * weights["popularity"]
        + vote_score * weights["votes"]
    )

    return {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "year": year,
        "score": round(score, 6),
        "score_breakdown": {
            "title_similarity": round(title_similarity, 6),
            "year_proximity": round(year_score, 6),
            "runtime_fit": round(runtime_score, 6),
            "season_hint": round(season_score, 6),
            "popularity": round(popularity_score, 6),
            "votes": round(vote_score, 6),
            "weights": weights,
                "hint_year": hint_year,
                "hint_season": hint_season,
                "avg_runtime_minutes": avg_runtime_minutes,
                "query": query,
                "query_source": query_source,
                "first_air_date": first_air_date,
                "release_date": release_date,
            },
        "selected": 0,
        "manual_override": 0,
    }


def _extract_candidate_year(item: dict[str, Any]) -> int | None:
    date_str = item.get("first_air_date") or item.get("release_date")
    if not isinstance(date_str, str) or len(date_str) < 4:
        return None
    try:
        return int(date_str[:4])
    except ValueError:
        return None


def _score_year(hint_year: int | None, candidate_year: int | None) -> float:
    if hint_year is None or candidate_year is None:
        return 0.5
    delta = abs(hint_year - candidate_year)
    if delta == 0:
        return 1.0
    if delta == 1:
        return 0.8
    if delta <= 3:
        return 0.5
    if delta <= 6:
        return 0.25
    return 0.0


def _score_season_hint(hint_season: int | None, title: str, item: dict[str, Any]) -> float:
    if hint_season is None:
        return 0.5
    title_l = title.lower()
    patterns = [
        fr"\bseason\s*{hint_season}\b",
        fr"\bs{hint_season}\b",
    ]
    if any(re.search(p, title_l) for p in patterns):
        return 1.0
    # mildly positive if series exists but no explicit season in title
    return 0.55 if item.get("name") else 0.35


def _score_runtime_hint(avg_runtime_minutes: float | None, item: dict[str, Any]) -> float:
    """
    Coarse prior used before the real runtime is known.

    Search results carry no runtime, so this can only say "a 25 minute disc
    looks episodic". _rescore_with_actual_runtimes replaces this with a real
    comparison for the leading candidates once the detail endpoint is queried.
    """
    if avg_runtime_minutes is None:
        return 0.5
    # Kids episodic DVDs often 6-30min episodes.
    if avg_runtime_minutes <= 0:
        return 0.5
    if 5 <= avg_runtime_minutes <= 70:
        return 0.75
    if avg_runtime_minutes <= 120:
        return 0.55
    return 0.35


def _string_similarity(query: str, title: str) -> float:
    q = query.lower().strip()
    t = title.lower().strip()
    if not q or not t:
        return 0.0
    return SequenceMatcher(None, q, t).ratio()


def _query_has_specific_movie_signal(query: str) -> bool:
    normalized = _normalize_identify_query(query, "movie")
    if not normalized:
        return False
    if re.search(r"\b\d+\b", normalized):
        return True
    tokens = [token for token in normalized.split(" ") if token]
    return len(tokens) >= 6


def _has_movie_franchise_ambiguity(
    ranked: list[dict[str, Any]],
    requested_media_type: str,
) -> bool:
    if requested_media_type != "movie" or len(ranked) < 2:
        return False

    top = ranked[0]
    top_breakdown = top.get("score_breakdown") if isinstance(top.get("score_breakdown"), dict) else {}
    query = _normalize_identify_query(str(top_breakdown.get("query") or ""), "movie")
    if not query or _query_has_specific_movie_signal(query):
        return False

    hint_year = top_breakdown.get("hint_year")
    if isinstance(hint_year, int):
        return False

    top_title = _normalize_identify_query(str(top.get("title") or ""), "movie")
    top_score = float(top.get("score") or 0.0)
    if top_title != query:
        return False

    for candidate in ranked[1:]:
        if str(candidate.get("media_type") or "") != "movie":
            continue
        candidate_title = _normalize_identify_query(str(candidate.get("title") or ""), "movie")
        if not candidate_title or candidate_title == top_title:
            continue
        if not candidate_title.startswith(query):
            continue
        candidate_score = float(candidate.get("score") or 0.0)
        if (top_score - candidate_score) <= 0.08:
            return True
    return False


def _has_movie_same_title_year_ambiguity(
    ranked: list[dict[str, Any]],
    requested_media_type: str,
) -> bool:
    if requested_media_type != "movie" or len(ranked) < 2:
        return False

    top = ranked[0]
    top_breakdown = top.get("score_breakdown") if isinstance(top.get("score_breakdown"), dict) else {}
    hint_year = top_breakdown.get("hint_year")
    query = _normalize_identify_query(str(top_breakdown.get("query") or ""), "movie")
    if isinstance(hint_year, int) or _query_has_specific_movie_signal(query):
        return False

    top_title = _normalize_identify_query(str(top.get("title") or ""), "movie")
    if not top_title:
        return False

    same_title_candidates = [
        candidate
        for candidate in ranked
        if str(candidate.get("media_type") or "") == "movie"
        and _normalize_identify_query(str(candidate.get("title") or ""), "movie") == top_title
    ]
    if len(same_title_candidates) < 2:
        return False

    first = same_title_candidates[0]
    second = same_title_candidates[1]
    first_year = first.get("year")
    second_year = second.get("year")
    if first_year is None or second_year is None or first_year == second_year:
        return False

    first_score = float(first.get("score") or 0.0)
    second_score = float(second.get("score") or 0.0)
    return abs(first_score - second_score) <= 0.03


# A runtime this close is treated as identifying: PAL speedup and rounding
# account for a minute or two, nothing else does.
RUNTIME_PROOF_DELTA_MINUTES = 2.0
# ...and it only counts as proof if every rival with the same title is at
# least this much further away.
RUNTIME_PROOF_MARGIN_MINUTES = 5.0


def _same_title_lacks_runtime_proof(
    ranked: list[dict[str, Any]],
    requested_media_type: str,
) -> bool:
    """
    True when several candidates share the top title and runtime does not
    decisively single one out.

    A disc labelled "SINBAD" matches five different films actually titled
    "Sinbad", and the one the user wants (Sinbad: Legend of the Seven Seas)
    is not even among them -- TMDB's search does not return it. Scoring the
    ripped runtime against the survivors produced a confident pick of the
    wrong film, which is worse than asking: a wrong auto-selection puts a
    mis-named file on the NAS, while a question costs one click.

    Runtime may still resolve this, but only when it is genuinely decisive:
    "Robin Hood" is equally ambiguous by title, yet an 83-minute disc matches
    Disney's 1973 film exactly and nothing else close.
    """
    if requested_media_type != "movie" or len(ranked) < 2:
        return False

    top = ranked[0]
    top_title = _normalize_identify_query(str(top.get("title") or ""), "movie")
    if not top_title:
        return False

    same_title = [
        candidate
        for candidate in ranked
        if str(candidate.get("media_type") or "") == "movie"
        and _normalize_identify_query(str(candidate.get("title") or ""), "movie") == top_title
    ]
    if len(same_title) < 2:
        return False

    top_delta = _runtime_delta(top)
    if top_delta is None or top_delta > RUNTIME_PROOF_DELTA_MINUTES:
        # No runtime, or not close enough to be identifying.
        return True

    for rival in same_title[1:]:
        rival_delta = _runtime_delta(rival)
        if rival_delta is None:
            if _runtime_lookup_state(rival) == "unavailable":
                # TMDB holds no runtime for this entry -- usually an obscure or
                # unreleased title. Not a credible rival to an exact match.
                continue
            # Never checked, so it cannot be ruled out.
            return True
        if rival_delta - top_delta < RUNTIME_PROOF_MARGIN_MINUTES:
            return True

    return False


def _runtime_delta(candidate: dict[str, Any]) -> float | None:
    breakdown = candidate.get("score_breakdown")
    if not isinstance(breakdown, dict):
        return None
    value = breakdown.get("runtime_delta_minutes")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cached_tmdb_get(conn, cfg: AppConfig, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    params_sorted = {k: params[k] for k in sorted(params.keys())}
    cache_key = _cache_key(endpoint, params_sorted)
    cached = conn.execute(
        "SELECT response_json FROM tmdb_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    if cached:
        try:
            return json.loads(cached["response_json"])
        except json.JSONDecodeError:
            pass

    payload = _tmdb_get(cfg.tmdb_api_key, endpoint, params_sorted)
    conn.execute(
        """
        INSERT INTO tmdb_cache (cache_key, endpoint, params_json, response_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            response_json = excluded.response_json,
            created_at = excluded.created_at
        """,
        (
            cache_key,
            endpoint,
            json.dumps(params_sorted, ensure_ascii=True),
            json.dumps(payload, ensure_ascii=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return payload


def _cache_key(endpoint: str, params: dict[str, Any]) -> str:
    base = f"{endpoint}|{json.dumps(params, sort_keys=True, ensure_ascii=True)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _tmdb_get(api_key: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    query = dict(params)
    query["api_key"] = api_key
    url = f"{TMDB_BASE_URL}{endpoint}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url=url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise TmdbError(f"TMDB request failed HTTP {exc.code}: {endpoint}") from exc
    except urllib.error.URLError as exc:
        raise TmdbError(f"TMDB request failed: {exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise TmdbError("TMDB returned invalid JSON.") from exc


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[int, str]] = set()
    deduped: list[dict[str, Any]] = []
    for c in sorted(candidates, key=lambda x: x["score"], reverse=True):
        key = (int(c["tmdb_id"]), str(c["media_type"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped


def _should_auto_select_primary_candidate(
    ranked: list[dict[str, Any]],
    requested_media_type: str,
    threshold: float,
) -> bool:
    if not ranked:
        return False

    top = ranked[0]
    top_media_type = str(top.get("media_type") or "")
    top_score = float(top.get("score") or 0.0)
    top_breakdown = top.get("score_breakdown") if isinstance(top.get("score_breakdown"), dict) else {}
    title_similarity = float(top_breakdown.get("title_similarity") or 0.0)
    if top_media_type != requested_media_type:
        return False
    if _has_movie_franchise_ambiguity(ranked, requested_media_type):
        return False
    if _has_movie_same_title_year_ambiguity(ranked, requested_media_type):
        return False
    if _same_title_lacks_runtime_proof(ranked, requested_media_type):
        return False
    if top_score >= threshold:
        return True

    same_type = [candidate for candidate in ranked if str(candidate.get("media_type") or "") == requested_media_type]
    other_type = [candidate for candidate in ranked if str(candidate.get("media_type") or "") != requested_media_type]
    if len(same_type) != 1:
        return False
    if len(ranked) == 1:
        return title_similarity >= 0.68 and top_score >= 0.60
    if top_score < max(0.65, threshold - 0.06):
        return False
    if not other_type:
        return True

    runner_up = other_type[0]
    runner_up_score = float(runner_up.get("score") or 0.0)
    return (top_score - runner_up_score) >= 0.06


def _required_movie_slots(movie_mode: str | None) -> int:
    if movie_mode == "double_feature":
        return 2
    if movie_mode == "trilogy":
        return 3
    return 1


def list_selected_movie_slots(conn, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT slot_index, tmdb_id, title, year, rip_title_id, created_at, updated_at
        FROM job_selected_movies
        WHERE job_id = ?
        ORDER BY slot_index ASC
        """,
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _upsert_selected_movie_slot(
    conn,
    job_id: str,
    slot_index: int,
    tmdb_id: int,
    title: str,
    year: int | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO job_selected_movies (
            job_id, slot_index, tmdb_id, title, year, rip_title_id, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(job_id, slot_index) DO UPDATE SET
            tmdb_id = excluded.tmdb_id,
            title = excluded.title,
            year = excluded.year,
            updated_at = excluded.updated_at
        """,
        (job_id, slot_index, tmdb_id, title, year, now, now),
    )


def _persist_tmdb_candidates(conn, job_id: str, ranked: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM tmdb_candidates WHERE job_id = ?", (job_id,))
    for c in ranked:
        conn.execute(
            """
            INSERT INTO tmdb_candidates (
                job_id, tmdb_id, media_type, title, year, score, score_breakdown_json, selected, manual_override
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                int(c["tmdb_id"]),
                str(c["media_type"]),
                str(c["title"]),
                c["year"],
                float(c["score"]),
                json.dumps(c["score_breakdown"], ensure_ascii=True),
                int(c.get("selected", 0)),
                int(c.get("manual_override", 0)),
            ),
        )


def _mark_selected_candidate(conn, job_id: str, tmdb_id: int, media_type: str) -> None:
    conn.execute(
        "UPDATE tmdb_candidates SET selected = 0, manual_override = 0 WHERE job_id = ?",
        (job_id,),
    )
    conn.execute(
        """
        UPDATE tmdb_candidates
        SET selected = 1, manual_override = 0
        WHERE job_id = ? AND tmdb_id = ? AND media_type = ?
        """,
        (job_id, tmdb_id, media_type),
    )


def _upsert_selected_media(
    conn,
    job_id: str,
    media_type: str,
    tmdb_id: int,
    title: str,
    year: int | None,
    season_number: int | None,
    order_mode: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO job_selected_media (
            job_id, media_type, tmdb_id, title, year, season_number, order_mode, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            media_type = excluded.media_type,
            tmdb_id = excluded.tmdb_id,
            title = excluded.title,
            year = excluded.year,
            season_number = COALESCE(excluded.season_number, job_selected_media.season_number),
            order_mode = COALESCE(excluded.order_mode, job_selected_media.order_mode),
            updated_at = excluded.updated_at
        """,
        (
            job_id,
            media_type,
            int(tmdb_id),
            title,
            year,
            season_number,
            order_mode,
            now,
            now,
        ),
    )


def fetch_tmdb_tv_episodes(
    conn,
    cfg: AppConfig,
    tmdb_show_id: int,
    season_number: int,
) -> list[dict[str, Any]]:
    payload = _cached_tmdb_get(
        conn,
        cfg,
        endpoint=f"/tv/{tmdb_show_id}/season/{season_number}",
        params={},
    )
    eps = payload.get("episodes") if isinstance(payload, dict) else []
    if not isinstance(eps, list):
        return []
    out: list[dict[str, Any]] = []
    for ep in eps:
        number = ep.get("episode_number")
        ep_id = ep.get("id")
        if not isinstance(number, int) or not isinstance(ep_id, int):
            continue
        out.append(
            {
                "id": ep_id,
                "episode_number": number,
                "name": str(ep.get("name") or f"Episode {number}"),
                "runtime": ep.get("runtime"),
            }
        )
    return sorted(out, key=lambda x: x["episode_number"])

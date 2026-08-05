import argparse
from datetime import datetime, timezone
import json
import re
import sys
from pathlib import Path
from typing import Any

from autorippr.config import ConfigError, load_config
from autorippr.db import open_db
from autorippr.logger import configure_logging, get_logger
from autorippr.state import (
    InvalidTransitionError,
    create_job,
    get_job,
    list_job_logs,
    list_jobs,
    transition_job,
    update_job_disc_profile,
)
from autorippr.rip import RipError, discover_optical_drives, execute_rip_job
from autorippr.tmdb import TmdbError, fetch_tmdb_tv_episodes, identify_job_with_tmdb, search_job_with_tmdb_query, select_tmdb_candidate
from autorippr.mapper import (
    MappingError,
    analyze_dvd_menu,
    map_job_episodes,
    set_mapping_ignore,
    set_mapping_override,
    set_mapping_source_override,
)
from autorippr.splitter import SplitError, execute_splits, plan_splits_for_job, set_manual_split_timestamps
from autorippr.naming import NamingError, finalize_job_outputs, select_likely_movie_feature_rows
from autorippr.transfer import TransferError, transfer_job_outputs
from autorippr.pipeline import resume_incomplete_jobs, run_pipeline_for_job
from autorippr.job_ops import (
    JobDeleteError,
    cancel_job,
    clear_job_local_artifacts,
    clear_job_output_artifacts,
    delete_job,
    remap_job_remote_output,
)


def _load_json_file(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_text_file(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _job_has_local_artifacts(staging_root: str, job_id: str) -> bool:
    job_dir = Path(staging_root) / "jobs" / job_id
    artifact_dirs = [
        job_dir / "rip_output",
        job_dir / "split_output",
        job_dir / "finalized",
        job_dir / "menu_analysis",
        job_dir / "dvdnav_menu",
        job_dir / "dvd_arch_menu",
        job_dir / "ocr",
    ]
    return any(path.exists() for path in artifact_dirs)


def _parse_progress_keyvals(message: str, prefix: str) -> dict[str, float] | None:
    if not message.startswith(prefix):
        return None
    payload = message[len(prefix):].strip()
    parsed: dict[str, float] = {}
    for part in payload.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        try:
            parsed[key.strip()] = float(value.strip())
        except ValueError:
            continue
    return parsed


def _extract_expected_rip_size_mb(log_text: str) -> float | None:
    match = re.search(r"total size of all output files may reach as much as (\d+) megabytes", log_text, flags=re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1))


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_progress_state(
    cfg,
    job: dict[str, Any],
    logs: list[dict[str, Any]],
    output_rows: list[Any],
    job_id: str,
) -> dict[str, Any]:
    status = str(job.get("status") or "queued")
    media_type = str(job.get("media_type") or "movie")
    movie_stages = ["queued", "ripping", "identifying", "renaming", "copying", "done"]
    tv_stages = ["queued", "ripping", "identifying", "mapping", "splitting", "renaming", "copying", "done"]
    stage_order = movie_stages if media_type == "movie" else tv_stages
    current_index = stage_order.index(status) if status in stage_order else 0
    overall_fraction = current_index / max(1, (len(stage_order) - 1))
    progress = {
        "overall_fraction": overall_fraction,
        "stage_fraction": 0.0,
        "detail": None,
        "eta_seconds": None,
        "rate_mb_s": None,
        "current_mb": None,
        "total_mb": None,
        "kind": None,
    }

    if status == "ripping":
        rip_logs = [log for log in logs if str(log.get("message") or "").startswith("Rip heartbeat:")]
        latest = _parse_progress_keyvals(str(rip_logs[-1]["message"]), "Rip heartbeat:") if rip_logs else None
        previous = _parse_progress_keyvals(str(rip_logs[-2]["message"]), "Rip heartbeat:") if len(rip_logs) >= 2 else None
        log_text = _read_text_file(Path(cfg.staging_root) / "jobs" / job_id / "logs" / "makemkv.log")
        total_mb = _extract_expected_rip_size_mb(log_text)
        current_mb = float(latest.get("size_mb")) if latest and latest.get("size_mb") is not None else None
        rate_mb_s = None
        eta_seconds = None
        if latest and previous:
            # Heartbeats are emitted every ~15s, so use that fixed interval for stable UI math.
            delta = 15.0
            rate_mb_s = max(0.0, (float(latest.get("size_mb", 0.0)) - float(previous.get("size_mb", 0.0))) / delta)
            if total_mb and current_mb is not None and rate_mb_s > 0:
                eta_seconds = max(0.0, (total_mb - current_mb) / rate_mb_s)
        stage_fraction = 0.0
        if total_mb and current_mb is not None and total_mb > 0:
            stage_fraction = min(0.99, max(0.0, current_mb / total_mb))
        elif current_mb is not None:
            stage_fraction = 0.5
        progress.update(
            {
                "overall_fraction": 0.10 + (0.45 if media_type == "movie" else 0.35) * stage_fraction,
                "stage_fraction": stage_fraction,
                "detail": f"Ripping {current_mb:.1f} / {total_mb:.1f} MB" if current_mb is not None and total_mb else ("Ripping in progress" if current_mb is None else f"Ripping {current_mb:.1f} MB"),
                "eta_seconds": eta_seconds,
                "rate_mb_s": rate_mb_s,
                "current_mb": current_mb,
                "total_mb": total_mb,
                "kind": "ripping",
            }
        )
        return progress

    if status == "copying":
        transfer_logs = [log for log in logs if str(log.get("message") or "").startswith("Transfer heartbeat:")]
        latest = _parse_progress_keyvals(str(transfer_logs[-1]["message"]), "Transfer heartbeat:") if transfer_logs else None
        total_bytes = 0
        copied_bytes = 0
        finalize_root = Path(cfg.staging_root) / "jobs" / job_id / "finalized"
        for row in output_rows:
            local_path = Path(str(row["local_path"]))
            if local_path.exists():
                size = local_path.stat().st_size
                total_bytes += size
                if row["transfer_status"] == "done" and row["nas_path"]:
                    copied_bytes += size
                else:
                    try:
                        relative = local_path.relative_to(finalize_root)
                        base = "Movies" if media_type == "movie" else "TVShows"
                        temp_dest = Path(cfg.nas_root) / base / relative
                        temp_path = temp_dest.with_suffix(temp_dest.suffix + ".part")
                        if temp_path.exists():
                            copied_bytes += temp_path.stat().st_size
                    except (ValueError, OSError):
                        pass
        total_mb = total_bytes / (1024 * 1024) if total_bytes else None
        current_mb = copied_bytes / (1024 * 1024) if total_bytes else None
        stage_fraction = min(0.99, copied_bytes / total_bytes) if total_bytes else 0.0
        progress.update(
            {
                "overall_fraction": (0.85 if media_type == "movie" else 0.92) + (0.15 if media_type == "movie" else 0.08) * stage_fraction,
                "stage_fraction": stage_fraction,
                "detail": f"Copying {current_mb:.1f} / {total_mb:.1f} MB" if current_mb is not None and total_mb else "Copying in progress",
                "eta_seconds": latest.get("eta_seconds") if latest else None,
                "rate_mb_s": latest.get("rate_mb_s") if latest else None,
                "current_mb": current_mb,
                "total_mb": total_mb,
                "kind": "copying",
            }
        )
        return progress

    if status == "done":
        progress["overall_fraction"] = 1.0
        progress["stage_fraction"] = 1.0
        progress["detail"] = "Completed"
        return progress

    return progress


def _sync_movie_job_status(job: dict[str, Any], selected_media: Any) -> dict[str, Any]:
    if not job:
        return job
    if str(job.get("media_type") or "tv") != "movie":
        return job
    if str(job.get("status")) == "mapping":
        job = dict(job)
        job["status"] = "renaming"
        job["current_stage"] = "renaming"
    if str(job.get("status")) == "identifying" and selected_media is not None:
        job = dict(job)
        job["status"] = "renaming"
        job["current_stage"] = "renaming"
    return job


def _build_review_state(
    *,
    cfg,
    job: dict[str, Any],
    logs: list[dict[str, Any]],
    selected_media: Any,
    selected_movies: list[dict[str, Any]],
    tmdb_rows: list[Any],
    mapping_rows: list[Any],
    rip_title_rows: list[Any],
    bundle_association: Any,
    tmdb_threshold: float,
) -> dict[str, Any]:
    top_candidate = dict(tmdb_rows[0]) if tmdb_rows else None
    movie_mode = str(job.get("movie_mode") or "single")
    required_movie_slots = 3 if movie_mode == "trilogy" else 2 if movie_mode == "double_feature" else 1
    multi_movie_mode = str(job.get("media_type") or "tv") == "movie" and movie_mode != "single"
    tmdb_needed = job.get("status") == "identifying" and (
        len(selected_movies) < required_movie_slots if multi_movie_mode else selected_media is None
    )
    tmdb_reason = None
    if tmdb_needed:
        if multi_movie_mode:
            tmdb_reason = (
                f"Select movies in order: {len(selected_movies)}/{required_movie_slots} slots filled."
            )
        elif top_candidate:
            tmdb_reason = (
                f"Top TMDB candidate score {float(top_candidate['score']):.2f} "
                f"is below threshold {tmdb_threshold:.2f}."
            )
        else:
            tmdb_reason = "No TMDB media has been selected for this job yet."

    low_confidence = [
        dict(row)
        for row in mapping_rows
        if row["confidence"] is None or float(row["confidence"]) < 0.85
    ]
    bundle_gate = bundle_association.get("confidence_gate") if isinstance(bundle_association, dict) else None
    mapping_needed = bool(low_confidence)
    mapping_reason = None
    if not mapping_needed and isinstance(bundle_gate, dict) and bundle_gate.get("ok") is False:
        mapping_needed = True
        mapping_reason = "Bundle association confidence gate is not satisfied."
    elif low_confidence:
        mapping_reason = (
            f"{len(low_confidence)} mapping(s) are below the 0.85 confidence gate."
        )

    rip_needed = False
    rip_reason = None
    rip_details: list[str] = []
    makemkv_log = _read_text_file(Path(cfg.staging_root) / "jobs" / str(job["id"]) / "logs" / "makemkv.log")
    if "MEDIUM ERROR" in makemkv_log or "UNCORRECTABLE ERROR" in makemkv_log:
        rip_needed = True
        rip_reason = "MakeMKV reported a disc read error during rip."
        rip_details.append("MakeMKV log contains MEDIUM ERROR / UNCORRECTABLE ERROR.")
    if str(job.get("media_type") or "tv") == "movie" and rip_title_rows:
        main_rows = select_likely_movie_feature_rows(rip_title_rows)
        if not main_rows and len(rip_title_rows) > 1:
            rip_needed = True
            rip_reason = "Only short extra/trailer titles were ripped; no feature-length movie file was found."
            rip_details.append(
                "Movie job has multiple rip titles, but none are at least 45 minutes long."
            )
    if str(job.get("status")) == "ripping":
        rip_heartbeats = [
            log for log in logs
            if str(log.get("message") or "").startswith("Rip heartbeat:")
        ]
        if rip_heartbeats:
            recent = rip_heartbeats[-4:]
            parsed = [
                (
                    _parse_iso_timestamp(log.get("timestamp")),
                    _parse_progress_keyvals(str(log.get("message")), "Rip heartbeat:"),
                )
                for log in recent
            ]
            parsed = [(ts, data) for ts, data in parsed if ts is not None and data is not None]
            if len(parsed) >= 3:
                first_ts = parsed[0][0]
                last_ts = parsed[-1][0]
                elapsed = (last_ts - first_ts).total_seconds()
                sizes = [float(data.get("size_mb", 0.0)) for _, data in parsed]
                if elapsed >= 45 and max(sizes) - min(sizes) < 1.0:
                    rip_needed = True
                    rip_reason = "Rip appears stalled: MakeMKV has not increased output size for about a minute."
                    rip_details.append(
                        f"Recent rip heartbeats stayed around {sizes[-1]:.1f} MB for {int(elapsed)}s."
                    )
        start_rip_log = next((log for log in logs if str(log.get("message") or "").startswith("Starting MakeMKV rip to ")), None)
        latest_job_update = _parse_iso_timestamp(str(job.get("updated_at") or ""))
        start_rip_ts = _parse_iso_timestamp(start_rip_log.get("timestamp")) if start_rip_log else None
        if start_rip_ts and latest_job_update:
            quiet_seconds = (latest_job_update - start_rip_ts).total_seconds()
            if quiet_seconds >= 60 and not rip_heartbeats:
                rip_needed = True
                rip_reason = "Rip appears stalled: MakeMKV started but no heartbeat/progress was recorded."
                rip_details.append(
                    f"No rip heartbeat was recorded for {int(quiet_seconds)}s after rip start."
                )

    return {
        "rip": {
            "needed": rip_needed,
            "reason": rip_reason,
            "details": rip_details,
            "threshold": 0,
        },
        "tmdb": {
            "needed": tmdb_needed,
            "reason": tmdb_reason,
            "threshold": tmdb_threshold,
            "candidate_count": len(tmdb_rows),
            "top_candidate": top_candidate,
            "required_slots": required_movie_slots if multi_movie_mode else 1,
            "selected_slots": len(selected_movies) if multi_movie_mode else (1 if selected_media else 0),
        },
        "mapping": {
            "needed": mapping_needed,
            "reason": mapping_reason,
            "threshold": 0.85,
            "low_confidence_count": len(low_confidence),
            "low_confidence_mappings": low_confidence,
            "bundle_confidence_gate": bundle_gate,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auto-Ripper foundation CLI")
    default_config = Path(__file__).parent / "config.local.json"
    if not default_config.exists():
        default_config = Path(__file__).parent / "config.json"
    parser.add_argument(
        "--config",
        default=str(default_config),
        help="Path to config file (default: app\\config.local.json if present, else app\\config.json)",
    )

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("validate-config", help="Validate configuration and exit")

    job = sub.add_parser("job", help="Job operations")
    job_sub = job.add_subparsers(dest="job_command")

    create = job_sub.add_parser("create", help="Create test job")
    create.add_argument("--disc-label", default="")
    create.add_argument("--media-type", default="tv", choices=["tv", "movie"])
    create.add_argument("--movie-mode", default="single", choices=["single", "double_feature", "trilogy"])
    create.add_argument("--disc-scope", default=None, choices=["full_season", "partial_season", "special", "custom"])
    create.add_argument("--season-number", type=int, default=None)
    create.add_argument("--episode-range-start", type=int, default=None)
    create.add_argument("--episode-range-end", type=int, default=None)

    profile = job_sub.add_parser("set-profile", help="Update disc scope/season/range")
    profile.add_argument("job_id")
    profile.add_argument("--disc-scope", default=None, choices=["full_season", "partial_season", "special", "custom"])
    profile.add_argument("--movie-mode", default=None, choices=["single", "double_feature", "trilogy"])
    profile.add_argument("--season-number", type=int, default=None)
    profile.add_argument("--episode-range-start", type=int, default=None)
    profile.add_argument("--episode-range-end", type=int, default=None)

    advance = job_sub.add_parser("advance", help="Advance job state")
    advance.add_argument("job_id")
    advance.add_argument(
        "to_status",
        choices=[
            "queued",
            "ripping",
            "identifying",
            "mapping",
            "splitting",
            "renaming",
            "copying",
            "done",
            "error",
        ],
    )
    advance.add_argument("--error-message", default=None)

    show = job_sub.add_parser("show", help="Show job + logs")
    show.add_argument("job_id")

    snapshot = job_sub.add_parser("snapshot", help="Show full job state for frontend use")
    snapshot.add_argument("job_id")

    job_sub.add_parser("list", help="List jobs")
    job_delete = job_sub.add_parser("delete", help="Delete job and its staged artifacts")
    job_delete.add_argument("job_id")
    job_cancel = job_sub.add_parser("cancel", help="Cancel an in-progress job")
    job_cancel.add_argument("job_id")
    job_clear_local = job_sub.add_parser("clear-local", help="Clear local artifacts while keeping the job")
    job_clear_local.add_argument("job_id")
    job_rebuild_output = job_sub.add_parser("rebuild-output", help="Rebuild local/NAS outputs from current selections")
    job_rebuild_output.add_argument("job_id")
    job_remap_remote = job_sub.add_parser("remap-remote", help="Move an existing NAS output to the current TMDB path")
    job_remap_remote.add_argument("job_id")

    rip = sub.add_parser("rip", help="Rip workflow commands")
    rip_sub = rip.add_subparsers(dest="rip_command")

    rip_drives = rip_sub.add_parser("drives", help="List optical drives/disc status")
    rip_drives.set_defaults(_cmd="rip_drives")

    rip_run = rip_sub.add_parser("run", help="Run rip on a job")
    rip_run.add_argument("job_id")
    rip_run.add_argument("--disc-index", type=int, default=0)
    rip_run.add_argument("--mock", action="store_true")
    rip_run.set_defaults(_cmd="rip_run")

    rip_titles = rip_sub.add_parser("titles", help="List persisted rip titles for a job")
    rip_titles.add_argument("job_id")
    rip_titles.set_defaults(_cmd="rip_titles")

    tmdb = sub.add_parser("tmdb", help="TMDB identification commands")
    tmdb_sub = tmdb.add_subparsers(dest="tmdb_command")

    tmdb_identify = tmdb_sub.add_parser("identify", help="Identify TMDB candidates for a job")
    tmdb_identify.add_argument("job_id")
    tmdb_identify.set_defaults(_cmd="tmdb_identify")

    tmdb_candidates = tmdb_sub.add_parser("candidates", help="List TMDB candidates for a job")
    tmdb_candidates.add_argument("job_id")
    tmdb_candidates.set_defaults(_cmd="tmdb_candidates")

    tmdb_search = tmdb_sub.add_parser("search", help="Search TMDB candidates with a manual query")
    tmdb_search.add_argument("job_id")
    tmdb_search.add_argument("query")
    tmdb_search.set_defaults(_cmd="tmdb_search")

    tmdb_select = tmdb_sub.add_parser("select", help="Manually select TMDB candidate")
    tmdb_select.add_argument("job_id")
    tmdb_select.add_argument("media_type", choices=["tv", "movie"])
    tmdb_select.add_argument("tmdb_id", type=int)
    tmdb_select.add_argument("--slot-index", type=int, default=None)
    tmdb_select.set_defaults(_cmd="tmdb_select")

    mapping = sub.add_parser("mapping", help="Episode mapping commands")
    mapping_sub = mapping.add_subparsers(dest="mapping_command")
    mapping_analyze = mapping_sub.add_parser("analyze-menu", help="Analyze DVD menu and cache artifacts")
    mapping_analyze.add_argument("job_id")
    mapping_run = mapping_sub.add_parser("run", help="Generate episode mappings for a job")
    mapping_run.add_argument("job_id")
    mapping_override = mapping_sub.add_parser("override", help="Set manual mapping override")
    mapping_override.add_argument("mapping_id", type=int)
    mapping_override.add_argument("episode_start", type=int)
    mapping_override.add_argument("episode_end", type=int)
    mapping_ignore = mapping_sub.add_parser("ignore", help="Mark a mapping row as ignored/extras")
    mapping_ignore.add_argument("mapping_id", type=int)
    mapping_source = mapping_sub.add_parser("source-override", help="Set manual mapping source file override")
    mapping_source.add_argument("mapping_id", type=int)
    mapping_source.add_argument("rip_title_id", type=int)
    mapping_list = mapping_sub.add_parser("list", help="List mappings for a job")
    mapping_list.add_argument("job_id")

    split = sub.add_parser("split", help="Split planning/execution commands")
    split_sub = split.add_subparsers(dest="split_command")
    split_plan = split_sub.add_parser("plan", help="Plan split segments for combined episodes")
    split_plan.add_argument("job_id")
    split_run = split_sub.add_parser("run", help="Execute split segments")
    split_run.add_argument("job_id")
    split_override = split_sub.add_parser("override", help="Manual split timestamps")
    split_override.add_argument("split_plan_id", type=int)
    split_override.add_argument("--start", type=float, default=None)
    split_override.add_argument("--end", type=float, default=None)
    split_list = split_sub.add_parser("list", help="List split plans for a job")
    split_list.add_argument("job_id")

    finalize = sub.add_parser("finalize", help="Finalize Plex naming and local placement")
    finalize.add_argument("job_id")

    transfer = sub.add_parser("transfer", help="Copy finalized files to NAS")
    transfer.add_argument("job_id")

    pipeline = sub.add_parser("pipeline", help="Run/resume full pipeline")
    pipeline_sub = pipeline.add_subparsers(dest="pipeline_command")
    pipeline_run = pipeline_sub.add_parser("run", help="Run from current stage")
    pipeline_run.add_argument("job_id")
    pipeline_run.add_argument("--mock-rip", action="store_true")
    pipeline_resume = pipeline_sub.add_parser("resume-all", help="Resume all incomplete jobs")
    pipeline_resume.add_argument("--mock-rip", action="store_true")

    gui = sub.add_parser("gui", help="Launch simple local GUI")
    gui.add_argument("--refresh-seconds", type=int, default=3)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(cfg.log_path, level="INFO")
    log = get_logger("cli")
    conn = open_db(cfg.db_path)

    if args.command in (None, "validate-config"):
        print("Configuration valid.")
        print(f"DB: {cfg.db_path}")
        print(f"Log: {cfg.log_path}")
        return 0

    if args.command == "job":
        if args.job_command == "create":
            job_id = create_job(
                conn,
                disc_label=args.disc_label,
                media_type=args.media_type,
                movie_mode=args.movie_mode,
                disc_scope=args.disc_scope,
                season_number=args.season_number,
                episode_range_start=args.episode_range_start,
                episode_range_end=args.episode_range_end,
            )
            print(job_id)
            return 0

        if args.job_command == "set-profile":
            update_job_disc_profile(
                conn,
                args.job_id,
                args.disc_scope,
                args.movie_mode,
                args.season_number,
                args.episode_range_start,
                args.episode_range_end,
            )
            print(f"Updated disc profile for {args.job_id}")
            return 0

        if args.job_command == "advance":
            try:
                transition_job(conn, args.job_id, args.to_status, args.error_message)
            except InvalidTransitionError as exc:
                print(f"State error: {exc}", file=sys.stderr)
                return 3
            print(f"{args.job_id} -> {args.to_status}")
            return 0

        if args.job_command == "show":
            job = get_job(conn, args.job_id)
            if job is None:
                print("Job not found.", file=sys.stderr)
                return 4
            logs = list_job_logs(conn, args.job_id)
            print(json.dumps({"job": job, "logs": logs}, indent=2))
            return 0

        if args.job_command == "snapshot":
            job = get_job(conn, args.job_id)
            if job is None:
                print("Job not found.", file=sys.stderr)
                return 4
            logs = list_job_logs(conn, args.job_id)
            tmdb_rows = conn.execute(
                """
                SELECT tmdb_id, media_type, title, year, score, score_breakdown_json, selected, manual_override
                FROM tmdb_candidates
                WHERE job_id = ?
                ORDER BY score DESC, id ASC
                """,
                (args.job_id,),
            ).fetchall()
            mapping_rows = conn.execute(
                """
                SELECT
                    em.id,
                    em.rip_title_id,
                    rt.title_id,
                    rt.source_file,
                    em.season_number,
                    em.episode_start,
                    em.episode_end,
                    em.tmdb_episode_ids_json,
                    em.episode_titles_json,
                    em.confidence,
                    em.reason,
                    em.manual_override,
                    em.needs_split
                FROM episode_mappings em
                LEFT JOIN rip_titles rt ON rt.id = em.rip_title_id
                WHERE em.job_id = ?
                ORDER BY em.id ASC
                """,
                (args.job_id,),
            ).fetchall()
            split_rows = conn.execute(
                """
                SELECT id, mapping_id, source_file, segment_index, start_seconds, end_seconds,
                       chapter_start, chapter_end, output_file, status, error_message
                FROM split_plans
                WHERE job_id = ?
                ORDER BY id ASC
                """,
                (args.job_id,),
            ).fetchall()
            output_rows = conn.execute(
                """
                SELECT id, local_path, nas_path, transfer_status, transfer_attempts, last_error
                FROM outputs
                WHERE job_id = ?
                ORDER BY id ASC
                """,
                (args.job_id,),
            ).fetchall()
            rip_title_rows = conn.execute(
                """
                SELECT id, title_id, duration_seconds, chapter_count, source_file, raw_metadata_json
                FROM rip_titles
                WHERE job_id = ?
                ORDER BY id ASC
                """,
                (args.job_id,),
            ).fetchall()
            selected_media = conn.execute(
                """
                SELECT media_type, tmdb_id, title, year, season_number, order_mode, created_at, updated_at
                FROM job_selected_media
                WHERE job_id = ?
                LIMIT 1
                """,
                (args.job_id,),
            ).fetchone()
            job = _sync_movie_job_status(job, selected_media)
            selected_movies = conn.execute(
                """
                SELECT slot_index, tmdb_id, title, year, rip_title_id, created_at, updated_at
                FROM job_selected_movies
                WHERE job_id = ?
                ORDER BY slot_index ASC
                """,
                (args.job_id,),
            ).fetchall()
            season_episodes = []
            all_season_episodes = []
            if selected_media and str(selected_media["media_type"]) == "tv":
                job_season = job.get("season_number")
                selected_season = selected_media["season_number"]
                season_number = int(job_season or selected_season or 1)
                all_season_episodes = fetch_tmdb_tv_episodes(conn, cfg, int(selected_media["tmdb_id"]), season_number)
                season_episodes = list(all_season_episodes)
                if job.get("episode_range_start") is not None and job.get("episode_range_end") is not None:
                    start = int(job["episode_range_start"])
                    end = int(job["episode_range_end"])
                    season_episodes = [
                        ep for ep in season_episodes
                        if start <= int(ep["episode_number"]) <= end
                    ]
            staging_job_root = Path(cfg.staging_root) / "jobs" / args.job_id
            menu_analysis = _load_json_file(staging_job_root / "menu_analysis" / "menu_analysis.json")
            bundle_association = _load_json_file(staging_job_root / "menu_analysis" / "bundle_association.json")
            dvdnav_menu = _load_json_file(staging_job_root / "dvdnav_menu" / "dvdnav_menu.json")
            review_state = _build_review_state(
                cfg=cfg,
                job=job,
                logs=logs,
                selected_media=selected_media,
                selected_movies=[dict(r) for r in selected_movies],
                tmdb_rows=tmdb_rows,
                mapping_rows=mapping_rows,
                rip_title_rows=rip_title_rows,
                bundle_association=bundle_association,
                tmdb_threshold=float(cfg.tmdb_confidence_threshold),
            )
            progress_state = _build_progress_state(
                cfg=cfg,
                job=job,
                logs=logs,
                output_rows=output_rows,
                job_id=args.job_id,
            )
            print(
                json.dumps(
                    {
                        "job": job,
                        "logs": logs,
                        "selected_media": dict(selected_media) if selected_media else None,
                        "selected_movies": [dict(r) for r in selected_movies],
                        "season_episodes": season_episodes,
                        "all_season_episodes": all_season_episodes,
                        "tmdb_candidates": [dict(r) for r in tmdb_rows],
                        "episode_mappings": [dict(r) for r in mapping_rows],
                        "split_plans": [dict(r) for r in split_rows],
                        "outputs": [dict(r) for r in output_rows],
                        "rip_titles": [dict(r) for r in rip_title_rows],
                        "menu_analysis": menu_analysis,
                        "bundle_association": bundle_association,
                        "dvdnav_menu": dvdnav_menu,
                        "review_state": review_state,
                        "progress_state": progress_state,
                    },
                    indent=2,
                )
            )
            return 0

        if args.job_command == "list":
            jobs = list_jobs(conn)
            for job in jobs:
                job["has_local_artifacts"] = _job_has_local_artifacts(cfg.staging_root, str(job["id"]))
            print(json.dumps({"jobs": jobs}, indent=2))
            return 0

        if args.job_command == "delete":
            try:
                result = delete_job(conn, cfg.staging_root, args.job_id)
            except JobDeleteError as exc:
                print(f"Job delete error: {exc}", file=sys.stderr)
                return 12
            print(json.dumps(result, indent=2))
            return 0

        if args.job_command == "cancel":
            try:
                result = cancel_job(conn, args.job_id)
            except JobDeleteError as exc:
                print(f"Job cancel error: {exc}", file=sys.stderr)
                return 12
            print(json.dumps(result, indent=2))
            return 0

        if args.job_command == "clear-local":
            try:
                result = clear_job_local_artifacts(conn, cfg.staging_root, args.job_id)
            except JobDeleteError as exc:
                print(f"Job cleanup error: {exc}", file=sys.stderr)
                return 12
            print(json.dumps(result, indent=2))
            return 0

        if args.job_command == "rebuild-output":
            job = get_job(conn, args.job_id)
            if job is None:
                print("Job not found.", file=sys.stderr)
                return 4
            try:
                cleanup = clear_job_output_artifacts(conn, args.job_id)
                manifest = finalize_job_outputs(conn, cfg, args.job_id)
                transfer = transfer_job_outputs(conn, cfg, args.job_id)
                if not transfer["errors"]:
                    conn.execute(
                        "UPDATE jobs SET status = 'done', current_stage = 'done' WHERE id = ?",
                        (args.job_id,),
                    )
                    conn.commit()
                print(json.dumps({"cleanup": cleanup, "manifest": manifest, "transfer": transfer}, indent=2))
                return 0
            except (JobDeleteError, NamingError, TransferError) as exc:
                print(f"Rebuild output error: {exc}", file=sys.stderr)
                return 12

        if args.job_command == "remap-remote":
            try:
                result = remap_job_remote_output(conn, cfg, args.job_id)
            except JobDeleteError as exc:
                print(f"Remote remap error: {exc}", file=sys.stderr)
                return 12
            print(json.dumps(result, indent=2))
            return 0

    if args.command == "rip":
        if args.rip_command == "drives":
            print(json.dumps({"drives": discover_optical_drives()}, indent=2))
            return 0

        if args.rip_command == "titles":
            rows = conn.execute(
                """
                SELECT id, job_id, title_id, duration_seconds, chapter_count, source_file
                FROM rip_titles
                WHERE job_id = ?
                ORDER BY title_id ASC, id ASC
                """,
                (args.job_id,),
            ).fetchall()
            print(json.dumps({"rip_titles": [dict(r) for r in rows]}, indent=2))
            return 0

        if args.rip_command == "run":
            job = get_job(conn, args.job_id)
            if job is None:
                print("Job not found.", file=sys.stderr)
                return 4

            try:
                transition_job(conn, args.job_id, "ripping")
                result = execute_rip_job(
                    conn=conn,
                    cfg=cfg,
                    job_id=args.job_id,
                    disc_index=args.disc_index,
                    mock=args.mock,
                )
                transition_job(conn, args.job_id, "identifying")
                print(json.dumps(result, indent=2))
                return 0
            except InvalidTransitionError as exc:
                print(f"State error: {exc}", file=sys.stderr)
                return 3
            except RipError as exc:
                message = str(exc)
                try:
                    transition_job(conn, args.job_id, "error", message)
                except InvalidTransitionError:
                    conn.execute(
                        "UPDATE jobs SET status = 'error', error_message = ?, updated_at = datetime('now') WHERE id = ?",
                        (message, args.job_id),
                    )
                    conn.commit()
                print(f"Rip error: {message}", file=sys.stderr)
                return 5

    if args.command == "tmdb":
        if args.tmdb_command == "candidates":
            rows = conn.execute(
                """
                SELECT job_id, tmdb_id, media_type, title, year, score, score_breakdown_json, selected, manual_override
                FROM tmdb_candidates
                WHERE job_id = ?
                ORDER BY score DESC, id ASC
                """,
                (args.job_id,),
            ).fetchall()
            print(json.dumps({"tmdb_candidates": [dict(r) for r in rows]}, indent=2))
            return 0

        if args.tmdb_command == "select":
            try:
                selected = select_tmdb_candidate(conn, args.job_id, args.tmdb_id, args.media_type, args.slot_index)
            except TmdbError as exc:
                print(f"TMDB error: {exc}", file=sys.stderr)
                return 6
            print(json.dumps({"selected": selected}, indent=2))
            return 0

        if args.tmdb_command == "search":
            try:
                result = search_job_with_tmdb_query(conn, cfg, args.job_id, args.query)
            except TmdbError as exc:
                print(f"TMDB error: {exc}", file=sys.stderr)
                return 6
            print(json.dumps(result, indent=2))
            return 0

        if args.tmdb_command == "identify":
            job = get_job(conn, args.job_id)
            if job is None:
                print("Job not found.", file=sys.stderr)
                return 4

            try:
                if job["status"] == "identifying":
                    pass
                elif job["status"] == "ripping":
                    transition_job(conn, args.job_id, "identifying")
                elif job["status"] in ("queued", "error"):
                    print(
                        "Job must be at least in identifying-ready state (typically after rip).",
                        file=sys.stderr,
                    )
                    return 3

                result = identify_job_with_tmdb(conn, cfg, args.job_id)

                # progress state if confidently identified
                if not result["needs_review"]:
                    current = get_job(conn, args.job_id)
                    if current and current["status"] == "identifying":
                        transition_job(conn, args.job_id, "mapping")
                print(json.dumps(result, indent=2))
                return 0
            except (TmdbError, InvalidTransitionError) as exc:
                message = str(exc)
                try:
                    current = get_job(conn, args.job_id)
                    if current and current["status"] == "identifying":
                        transition_job(conn, args.job_id, "error", message)
                except InvalidTransitionError:
                    pass
                print(f"TMDB error: {message}", file=sys.stderr)
                return 6

    if args.command == "mapping":
        if args.mapping_command == "analyze-menu":
            try:
                result = analyze_dvd_menu(conn, cfg, args.job_id)
                print(json.dumps(result, indent=2))
                return 0
            except MappingError as exc:
                print(f"Mapping error: {exc}", file=sys.stderr)
                return 7
        if args.mapping_command == "run":
            try:
                result = map_job_episodes(conn, cfg, args.job_id)
                current = get_job(conn, args.job_id)
                if current and current["status"] == "mapping":
                    if any(m.get("needs_split") for m in result["mappings"]):
                        transition_job(conn, args.job_id, "splitting")
                    else:
                        transition_job(conn, args.job_id, "renaming")
                print(json.dumps(result, indent=2))
                return 0
            except (MappingError, TmdbError, InvalidTransitionError) as exc:
                print(f"Mapping error: {exc}", file=sys.stderr)
                return 7

        if args.mapping_command == "override":
            mapping_row = conn.execute(
                "SELECT job_id FROM episode_mappings WHERE id = ? LIMIT 1",
                (args.mapping_id,),
            ).fetchone()
            if not mapping_row:
                print("Mapping not found.", file=sys.stderr)
                return 7
            selected_media = conn.execute(
                """
                SELECT jsm.tmdb_id, COALESCE(j.season_number, jsm.season_number, 1) AS season_number
                FROM job_selected_media jsm
                JOIN jobs j ON j.id = jsm.job_id
                WHERE jsm.job_id = ?
                LIMIT 1
                """,
                (mapping_row["job_id"],),
            ).fetchone()
            if not selected_media:
                print("Selected media not found for mapping override.", file=sys.stderr)
                return 7
            season_episodes = fetch_tmdb_tv_episodes(
                conn,
                cfg,
                int(selected_media["tmdb_id"]),
                int(selected_media["season_number"] or 1),
            )
            selected_numbers = list(range(args.episode_start, args.episode_end + 1))
            eps = [
                int(ep["id"])
                for ep in season_episodes
                if int(ep["episode_number"]) in selected_numbers
            ]
            if len(eps) != len(selected_numbers):
                print("Could not resolve TMDB episode IDs for the requested range.", file=sys.stderr)
                return 7
            payload = set_mapping_override(
                conn,
                cfg,
                mapping_id=args.mapping_id,
                episode_start=args.episode_start,
                episode_end=args.episode_end,
                tmdb_episode_ids=eps,
                reason="manual_override_cli",
            )
            print(json.dumps(payload, indent=2))
            return 0

        if args.mapping_command == "source-override":
            payload = set_mapping_source_override(
                conn,
                mapping_id=args.mapping_id,
                rip_title_id=args.rip_title_id,
                reason="manual_source_override_cli",
            )
            print(json.dumps(payload, indent=2))
            return 0

        if args.mapping_command == "ignore":
            payload = set_mapping_ignore(
                conn,
                mapping_id=args.mapping_id,
                reason="manual_ignore_cli",
            )
            print(json.dumps(payload, indent=2))
            return 0

        if args.mapping_command == "list":
            rows = conn.execute(
                """
                SELECT id, rip_title_id, season_number, episode_start, episode_end,
                       tmdb_episode_ids_json, episode_titles_json, confidence, reason, manual_override, needs_split
                FROM episode_mappings
                WHERE job_id = ?
                ORDER BY id ASC
                """,
                (args.job_id,),
            ).fetchall()
            print(json.dumps({"episode_mappings": [dict(r) for r in rows]}, indent=2))
            return 0

    if args.command == "split":
        if args.split_command == "plan":
            try:
                result = plan_splits_for_job(conn, args.job_id)
                print(json.dumps(result, indent=2))
                return 0
            except SplitError as exc:
                print(f"Split error: {exc}", file=sys.stderr)
                return 8

        if args.split_command == "run":
            try:
                result = execute_splits(conn, cfg, args.job_id)
                current = get_job(conn, args.job_id)
                if current and current["status"] == "splitting":
                    transition_job(conn, args.job_id, "renaming")
                print(json.dumps(result, indent=2))
                return 0
            except (SplitError, InvalidTransitionError) as exc:
                print(f"Split error: {exc}", file=sys.stderr)
                return 8

        if args.split_command == "override":
            try:
                result = set_manual_split_timestamps(
                    conn, args.split_plan_id, args.start, args.end
                )
                print(json.dumps(result, indent=2))
                return 0
            except SplitError as exc:
                print(f"Split error: {exc}", file=sys.stderr)
                return 8

        if args.split_command == "list":
            rows = conn.execute(
                """
                SELECT id, mapping_id, source_file, segment_index, start_seconds, end_seconds,
                       chapter_start, chapter_end, output_file, status
                FROM split_plans
                WHERE job_id = ?
                ORDER BY id ASC
                """,
                (args.job_id,),
            ).fetchall()
            print(json.dumps({"split_plans": [dict(r) for r in rows]}, indent=2))
            return 0

    if args.command == "finalize":
        try:
            result = finalize_job_outputs(conn, cfg, args.job_id)
            current = get_job(conn, args.job_id)
            if current and current["status"] == "renaming":
                transition_job(conn, args.job_id, "copying")
            print(json.dumps(result, indent=2))
            return 0
        except (NamingError, InvalidTransitionError) as exc:
            print(f"Finalize error: {exc}", file=sys.stderr)
            return 9

    if args.command == "transfer":
        try:
            result = transfer_job_outputs(conn, cfg, args.job_id)
            current = get_job(conn, args.job_id)
            if current and current["status"] == "copying" and not result["errors"]:
                transition_job(conn, args.job_id, "done")
            print(json.dumps(result, indent=2))
            return 0
        except (TransferError, InvalidTransitionError) as exc:
            print(f"Transfer error: {exc}", file=sys.stderr)
            return 10

    if args.command == "pipeline":
        if args.pipeline_command == "run":
            try:
                job = get_job(conn, args.job_id)
                if not job:
                    print("Job not found.", file=sys.stderr)
                    return 4
                result = run_pipeline_for_job(conn, cfg, args.job_id, mock_rip=args.mock_rip)
                print(json.dumps({"job_id": args.job_id, **result}, indent=2))
                return 0
            except (
                InvalidTransitionError,
                RipError,
                TmdbError,
                MappingError,
                SplitError,
                NamingError,
                TransferError,
            ) as exc:
                print(f"Pipeline error: {exc}", file=sys.stderr)
                return 11
        if args.pipeline_command == "resume-all":
            try:
                result = resume_incomplete_jobs(conn, cfg, mock_rip=args.mock_rip)
                print(json.dumps({"results": result}, indent=2))
                return 0
            except Exception as exc:
                print(f"Pipeline resume error: {exc}", file=sys.stderr)
                return 11

    if args.command == "gui":
        from autorippr.ui import launch_gui

        launch_gui(conn, cfg, refresh_seconds=args.refresh_seconds)
        return 0

    log.error("Unknown command")
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REQUIRED_KEYS = (
    "tmdb_api_key",
    "makemkv_path",
    "ffmpeg_path",
    "ffprobe_path",
    "staging_root",
    "nas_root",
)

ENV_OVERRIDES = {
    "tmdb_api_key": "TMDB_API_KEY",
    "makemkv_path": "MAKEMKV_PATH",
    "ffmpeg_path": "FFMPEG_PATH",
    "ffprobe_path": "FFPROBE_PATH",
    "staging_root": "STAGING_ROOT",
    "nas_root": "NAS_ROOT",
    "db_path": "DB_PATH",
    "log_path": "LOG_PATH",
}

VALID_LIBRARY_TYPES = {"tv", "movies", "both"}
VALID_ORDER_MODES = {"aired", "dvd", "absolute"}
VALID_COLLISION_POLICIES = {"skip", "overwrite"}
VALID_TITLE_SELECTION_MODES = {"auto", "all"}


class ConfigError(ValueError):
    pass


def _is_blank_or_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    normalized = value.strip()
    if not normalized:
        return True
    upper = normalized.upper()
    return upper.startswith("REPLACE_WITH_") or upper.startswith("YOUR_")


def _as_bool(value: Any, default: bool = False) -> bool:
    """
    Coerce a config value to bool, tolerating strings.

    The settings UI round-trips every field as text, so a checkbox can arrive
    as the string "false" -- which is truthy in Python. Parse it properly
    rather than trusting bool().
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off", ""):
            return False
    return default


@dataclass(frozen=True)
class AppConfig:
    tmdb_api_key: str
    makemkv_path: str
    ffmpeg_path: str
    ffprobe_path: str
    staging_root: str
    nas_root: str
    db_path: str
    log_path: str
    plex_library_type: str = "both"
    default_order_mode: str = "aired"
    min_episode_minutes: float = 10.0
    max_episode_minutes: float = 90.0
    show_overrides: dict[str, Any] = field(default_factory=dict)
    rip_timeout_seconds: int = 7200
    tmdb_confidence_threshold: float = 0.75
    transfer_retry_count: int = 3
    transfer_backoff_seconds: int = 3
    collision_policy: str = "skip"
    # "auto" picks the titles worth ripping from the disc scan; "all" restores
    # the previous behaviour of ripping every title MakeMKV reports.
    rip_title_selection: str = "auto"
    # Titles shorter than this are ignored entirely. Must be used identically
    # for the disc scan and the rip -- MakeMKV renumbers titles after
    # filtering, so a mismatch would shift the selected title IDs.
    rip_min_title_seconds: int = 120
    # Blu-ray playlist scans routinely take 1-3 minutes; the old hard-coded
    # 45s cap timed them out and discarded the title metadata.
    disc_scan_timeout_seconds: int = 300
    # Eject the disc when a rip finishes, so a migration session becomes
    # "swap disc, repeat" instead of "go click something".
    eject_after_rip: bool = False


def load_config(config_path: str) -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}. "
            "Create one from app\\config.example.json."
        )

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {config_path}: {exc}") from exc

    merged = dict(raw)
    for key, env_name in ENV_OVERRIDES.items():
        env_value = os.getenv(env_name)
        if env_value is not None and env_value != "":
            merged[key] = env_value

    missing: list[str] = []
    for key in REQUIRED_KEYS:
        val = merged.get(key)
        if _is_blank_or_placeholder(val):
            missing.append(key)

    if missing:
        detail = ", ".join(missing)
        raise ConfigError(
            f"Missing required config key(s): {detail}. "
            "Set them in config.json or matching environment variables."
        )

    library_type = str(merged.get("plex_library_type", "both")).strip().lower()
    if library_type not in VALID_LIBRARY_TYPES:
        raise ConfigError(
            "Invalid plex_library_type. Expected one of: tv, movies, both."
        )

    order_mode = str(merged.get("default_order_mode", "aired")).strip().lower()
    if order_mode not in VALID_ORDER_MODES:
        raise ConfigError(
            "Invalid default_order_mode. Expected one of: aired, dvd, absolute."
        )

    collision_policy = str(merged.get("collision_policy", "skip")).strip().lower()
    if collision_policy not in VALID_COLLISION_POLICIES:
        raise ConfigError(
            "Invalid collision_policy. Expected one of: skip, overwrite."
        )

    title_selection = str(merged.get("rip_title_selection", "auto")).strip().lower()
    if title_selection not in VALID_TITLE_SELECTION_MODES:
        raise ConfigError(
            "Invalid rip_title_selection. Expected one of: auto, all."
        )

    staging_root = str(merged["staging_root"]).strip()
    db_path = str(merged.get("db_path") or Path(staging_root) / "autorippr.db")
    log_path = str(merged.get("log_path") or Path(staging_root) / "autorippr.log")

    return AppConfig(
        tmdb_api_key=str(merged["tmdb_api_key"]).strip(),
        makemkv_path=str(merged["makemkv_path"]).strip(),
        ffmpeg_path=str(merged["ffmpeg_path"]).strip(),
        ffprobe_path=str(merged["ffprobe_path"]).strip(),
        staging_root=staging_root,
        nas_root=str(merged["nas_root"]).strip(),
        db_path=db_path,
        log_path=log_path,
        plex_library_type=library_type,
        default_order_mode=order_mode,
        min_episode_minutes=float(merged.get("min_episode_minutes", 10.0)),
        max_episode_minutes=float(merged.get("max_episode_minutes", 90.0)),
        show_overrides=dict(merged.get("show_overrides", {})),
        rip_timeout_seconds=int(merged.get("rip_timeout_seconds", 7200)),
        tmdb_confidence_threshold=float(merged.get("tmdb_confidence_threshold", 0.75)),
        transfer_retry_count=int(merged.get("transfer_retry_count", 3)),
        transfer_backoff_seconds=int(merged.get("transfer_backoff_seconds", 3)),
        collision_policy=collision_policy,
        rip_title_selection=title_selection,
        rip_min_title_seconds=int(merged.get("rip_min_title_seconds", 120)),
        disc_scan_timeout_seconds=int(merged.get("disc_scan_timeout_seconds", 300)),
        eject_after_rip=_as_bool(merged.get("eject_after_rip"), False),
    )


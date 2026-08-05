import sqlite3
from pathlib import Path


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    disc_label TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT 'tv',
    movie_mode TEXT NOT NULL DEFAULT 'single',
    disc_scope TEXT,
    season_number INTEGER,
    episode_range_start INTEGER,
    episode_range_end INTEGER,
    status TEXT NOT NULL,
    current_stage TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS rip_titles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    title_id INTEGER,
    duration_seconds REAL,
    chapter_count INTEGER,
    source_file TEXT,
    raw_metadata_json TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS tmdb_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    tmdb_id INTEGER,
    media_type TEXT,
    title TEXT,
    year INTEGER,
    score REAL,
    score_breakdown_json TEXT,
    selected INTEGER NOT NULL DEFAULT 0,
    manual_override INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS episode_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    rip_title_id INTEGER,
    season_number INTEGER,
    episode_start INTEGER,
    episode_end INTEGER,
    tmdb_episode_ids_json TEXT,
    episode_titles_json TEXT,
    confidence REAL,
    reason TEXT,
    manual_override INTEGER NOT NULL DEFAULT 0,
    needs_split INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(job_id) REFERENCES jobs(id),
    FOREIGN KEY(rip_title_id) REFERENCES rip_titles(id)
);

CREATE TABLE IF NOT EXISTS split_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    mapping_id INTEGER,
    source_file TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    start_seconds REAL,
    end_seconds REAL,
    chapter_start INTEGER,
    chapter_end INTEGER,
    output_file TEXT,
    error_message TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    FOREIGN KEY(job_id) REFERENCES jobs(id),
    FOREIGN KEY(mapping_id) REFERENCES episode_mappings(id)
);

CREATE TABLE IF NOT EXISTS outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    local_path TEXT NOT NULL,
    nas_path TEXT,
    checksum_sha256 TEXT,
    transfer_status TEXT NOT NULL DEFAULT 'pending',
    transfer_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS show_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_show_id INTEGER NOT NULL UNIQUE,
    order_mode TEXT NOT NULL DEFAULT 'aired',
    min_episode_minutes REAL,
    max_episode_minutes REAL
);

CREATE TABLE IF NOT EXISTS tmdb_cache (
    cache_key TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    params_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_selected_media (
    job_id TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    season_number INTEGER,
    order_mode TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS job_selected_movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    slot_index INTEGER NOT NULL,
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    rip_title_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, slot_index),
    FOREIGN KEY(job_id) REFERENCES jobs(id),
    FOREIGN KEY(rip_title_id) REFERENCES rip_titles(id)
);

CREATE TABLE IF NOT EXISTS finalized_manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS transfer_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(output_id) REFERENCES outputs(id)
);
"""


def open_db(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_SQL)
    _apply_best_effort_migrations(conn)
    conn.commit()
    return conn


def _apply_best_effort_migrations(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "current_stage" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN current_stage TEXT")
    if "disc_scope" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN disc_scope TEXT")
    if "movie_mode" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN movie_mode TEXT NOT NULL DEFAULT 'single'")
    if "season_number" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN season_number INTEGER")
    if "episode_range_start" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN episode_range_start INTEGER")
    if "episode_range_end" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN episode_range_end INTEGER")
    split_columns = {row["name"] for row in conn.execute("PRAGMA table_info(split_plans)").fetchall()}
    if "mapping_id" not in split_columns:
        conn.execute("ALTER TABLE split_plans ADD COLUMN mapping_id INTEGER")
    if "error_message" not in split_columns:
        conn.execute("ALTER TABLE split_plans ADD COLUMN error_message TEXT")
    mapping_columns = {row["name"] for row in conn.execute("PRAGMA table_info(episode_mappings)").fetchall()}
    if "episode_titles_json" not in mapping_columns:
        conn.execute("ALTER TABLE episode_mappings ADD COLUMN episode_titles_json TEXT")


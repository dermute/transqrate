"""SQLite persistence layer. Single connection guarded by a re-entrant lock -
plenty for the handful of writers (API, scanner, N workers) this app runs."""

import json
import sqlite3
import threading
from datetime import datetime, timezone

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    video_codec TEXT NOT NULL DEFAULT 'av1_qsv',
    preset TEXT NOT NULL DEFAULT 'veryslow',
    quality_mode TEXT NOT NULL DEFAULT 'icq',      -- 'icq' | 'vmaf'
    icq INTEGER NOT NULL DEFAULT 22,
    vmaf_target REAL NOT NULL DEFAULT 95.0,
    audio_codec TEXT NOT NULL DEFAULT 'libopus',   -- 'libopus' | 'copy'
    audio_kbps_per_channel INTEGER NOT NULL DEFAULT 64,
    audio_max_channels INTEGER NOT NULL DEFAULT 0, -- 0 = keep all
    max_resolution TEXT NOT NULL DEFAULT 'source', -- source|480p|720p|1080p|2160p
    container TEXT NOT NULL DEFAULT 'mkv',
    extra_video_args TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    output_path TEXT,                              -- NULL => transcode in place
    watch INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    profile_name TEXT,
    profile_json TEXT,
    input_path TEXT NOT NULL,
    output_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',        -- pending|analyzing|running|done|failed|cancelling|cancelled|skipped
    force INTEGER NOT NULL DEFAULT 0,              -- re-encode even if TRANSQRATE-tagged
    progress REAL NOT NULL DEFAULT 0,
    fps REAL,
    speed TEXT,
    eta_s INTEGER,
    chosen_icq INTEGER,
    vmaf_score REAL,
    size_in INTEGER,
    size_out INTEGER,
    error TEXT,
    log_file TEXT,
    created_at TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_input ON jobs(input_path);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER,
    mtime REAL,
    state TEXT NOT NULL DEFAULT 'candidate',       -- candidate|queued|done|tagged|skipped|failed
    job_id INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

SETTINGS_DEFAULTS = {
    "workers": "1",
    "scan_interval_s": "300",
    "extensions": "mkv",
    "min_file_mb": "10",
    "icq_min": "14",
    "icq_max": "35",
    "vmaf_sample_s": "20",
    "vmaf_min_samples": "6",
    "vmaf_max_samples": "6",
    "skip_if_larger": "1",
    "qsv_device": "",
}

SETTINGS_GROUPS = {
    "General": ["workers", "scan_interval_s", "extensions", "min_file_mb", "skip_if_larger"],
    "FFmpeg": ["qsv_device"],
    "VMAF search": ["icq_min", "icq_max", "vmaf_sample_s", "vmaf_min_samples",
                    "vmaf_max_samples"],
}

SETTINGS_META = {
    "workers": "Parallel transcode workers (keep at 1 per GPU)",
    "scan_interval_s": "Watch: seconds between folder scans",
    "extensions": "File extensions considered media (comma separated)",
    "min_file_mb": "Ignore files smaller than this (MB)",
    "icq_min": "VMAF search: lowest ICQ tried (best quality bound)",
    "icq_max": "VMAF search: highest ICQ tried (smallest file bound)",
    "vmaf_sample_s": "VMAF search: seconds per sample clip",
    "vmaf_min_samples": "VMAF search: minimum sample clips per file",
    "vmaf_max_samples": "VMAF search: maximum sample clips per file",
    "skip_if_larger": "If output is larger: discard it and move the original to the output folder instead (1/0)",
    "qsv_device": "DRM render node for QSV, e.g. /dev/dri/renderD129 (empty = auto)",
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _adopt_legacy_db() -> None:
    """Rename a pre-rename transqode.db (plus WAL/SHM) to the new name."""
    legacy = config.DB_PATH.parent / "transqode.db"
    if config.DB_PATH.exists() or not legacy.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        old = legacy.parent / (legacy.name + suffix)
        if old.exists():
            old.rename(config.DB_PATH.parent / (config.DB_PATH.name + suffix))


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_dirs()
            _adopt_legacy_db()
            _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
        return _conn


MIGRATIONS = [
    ("profiles", "audio_max_channels",
     "ALTER TABLE profiles ADD COLUMN audio_max_channels INTEGER NOT NULL DEFAULT 0"),
    ("profiles", "max_resolution",
     "ALTER TABLE profiles ADD COLUMN max_resolution TEXT NOT NULL DEFAULT 'source'"),
    ("jobs", "force",
     "ALTER TABLE jobs ADD COLUMN force INTEGER NOT NULL DEFAULT 0"),
]


def init() -> None:
    with _lock:
        conn = connect()
        conn.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(ddl)
        for k, v in SETTINGS_DEFAULTS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
        # abandon jobs that were mid-flight when the app was stopped
        conn.execute(
            "UPDATE jobs SET status='failed', error='interrupted by restart', finished_at=? "
            "WHERE status IN ('analyzing','running','cancelling')",
            (now(),),
        )
        conn.commit()
        seed_profiles()


def seed_profiles() -> None:
    with _lock:
        conn = connect()
        (count,) = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()
        if count:
            return
        ts = now()
        conn.execute(
            "INSERT INTO profiles(name, video_codec, preset, quality_mode, icq, vmaf_target,"
            " audio_codec, audio_kbps_per_channel, container, extra_video_args, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("AV1 QSV + Opus (ICQ 22)", "av1_qsv", "veryslow", "icq", 22, 95.0,
             "libopus", 64, "mkv", "-look_ahead_depth 100 -extbrc 1 -adaptive_i 1 -adaptive_b 1 -g 240", ts, ts),
        )
        conn.execute(
            "INSERT INTO profiles(name, video_codec, preset, quality_mode, icq, vmaf_target,"
            " audio_codec, audio_kbps_per_channel, container, extra_video_args, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("AV1 QSV + Opus (VMAF 95)", "av1_qsv", "veryslow", "vmaf", 22, 95.0,
             "libopus", 64, "mkv", "-look_ahead_depth 100 -extbrc 1 -adaptive_i 1 -adaptive_b 1 -g 240", ts, ts),
        )
        conn.commit()


def query(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        rows = connect().execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def query_one(sql: str, args: tuple = ()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> int:
    with _lock:
        conn = connect()
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------- settings

def get_settings() -> dict:
    s = dict(SETTINGS_DEFAULTS)
    for row in query("SELECT key, value FROM settings"):
        s[row["key"]] = row["value"]
    return s


def set_settings(values: dict) -> None:
    with _lock:
        conn = connect()
        for k, v in values.items():
            if k in SETTINGS_DEFAULTS:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, str(v)),
                )
        conn.commit()


# ---------------------------------------------------------------- jobs

def create_job(source: dict, profile: dict, input_path: str,
               force: bool = False) -> int | None:
    """Queue a job unless one is already pending/active for this path."""
    with _lock:
        existing = query_one(
            "SELECT id FROM jobs WHERE input_path=? AND status IN"
            " ('pending','analyzing','running','cancelling')",
            (input_path,),
        )
        if existing:
            return None
        job_id = execute(
            "INSERT INTO jobs(source_id, profile_name, profile_json, input_path,"
            " status, force, created_at) VALUES(?,?,?,?, 'pending', ?, ?)",
            (source["id"], profile["name"], json.dumps(profile), input_path,
             int(force), now()),
        )
        execute(
            "INSERT INTO files(path, state, job_id, updated_at) VALUES(?, 'queued', ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET state='queued', job_id=excluded.job_id,"
            " updated_at=excluded.updated_at",
            (input_path, job_id, now()),
        )
        return job_id


def claim_next_job() -> dict | None:
    with _lock:
        job = query_one("SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT 1")
        if not job:
            return None
        execute(
            "UPDATE jobs SET status='analyzing', started_at=? WHERE id=? AND status='pending'",
            (now(), job["id"]),
        )
        return query_one("SELECT * FROM jobs WHERE id=?", (job["id"],))


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id: int) -> dict | None:
    return query_one("SELECT * FROM jobs WHERE id=?", (job_id,))


def job_status(job_id: int) -> str | None:
    row = query_one("SELECT status FROM jobs WHERE id=?", (job_id,))
    return row["status"] if row else None


def set_file_state(path: str, state: str, job_id: int | None = None,
                   size: int | None = None, mtime: float | None = None) -> None:
    execute(
        "INSERT INTO files(path, size, mtime, state, job_id, updated_at) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET size=COALESCE(excluded.size, files.size),"
        " mtime=COALESCE(excluded.mtime, files.mtime), state=excluded.state,"
        " job_id=COALESCE(excluded.job_id, files.job_id), updated_at=excluded.updated_at",
        (path, size, mtime, state, job_id, now()),
    )

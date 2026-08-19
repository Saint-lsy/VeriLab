from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    spec_hash TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    title TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    parent_experiment_id TEXT REFERENCES experiments(id),
    git_commit TEXT NOT NULL,
    protocol_id TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    comparison_key TEXT NOT NULL,
    status TEXT NOT NULL,
    withdrawn INTEGER NOT NULL DEFAULT 0,
    evidence_health TEXT NOT NULL DEFAULT 'healthy',
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(policy_hash, spec_hash)
);

CREATE TRIGGER IF NOT EXISTS experiments_spec_immutable
BEFORE UPDATE OF spec_hash, spec_json, git_commit, protocol_id, policy_hash, comparison_key
ON experiments
BEGIN
    SELECT RAISE(ABORT, 'frozen experiment fields are immutable');
END;

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL UNIQUE REFERENCES experiments(id),
    ticket_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    command_json TEXT NOT NULL,
    run_dir TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    pid INTEGER,
    process_start_ticks INTEGER,
    command_fingerprint TEXT,
    started_at TEXT,
    heartbeat_at TEXT,
    stdout_size INTEGER NOT NULL DEFAULT 0,
    cpu_ticks INTEGER,
    gpu_sample_json TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    exit_receipt_sha256 TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TRIGGER IF NOT EXISTS runs_ticket_immutable
BEFORE UPDATE OF experiment_id, ticket_hash, command_json, run_dir, worktree_path
ON runs
BEGIN
    SELECT RAISE(ABORT, 'run ticket fields are immutable');
END;

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    attempt INTEGER NOT NULL,
    bundle_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    output_json TEXT,
    thread_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    UNIQUE(experiment_id, attempt)
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    value REAL NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('reported','computed','verified')),
    comparison_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(experiment_id, name, source)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    role TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    absolute_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    required INTEGER NOT NULL,
    object_path TEXT,
    health TEXT NOT NULL DEFAULT 'healthy',
    sealed_at TEXT NOT NULL,
    UNIQUE(run_id, role, relative_path)
);

CREATE TABLE IF NOT EXISTS codex_sessions (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN ('executor','reviewer')),
    thread_id TEXT,
    experiment_id TEXT REFERENCES experiments(id),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES codex_sessions(id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    prev_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TABLE IF NOT EXISTS leaderboard_entries (
    experiment_id TEXT PRIMARY KEY REFERENCES experiments(id),
    review_id TEXT NOT NULL REFERENCES reviews(id),
    comparison_key TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    score REAL NOT NULL,
    direction TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    withdrawn INTEGER NOT NULL DEFAULT 0,
    evidence_health TEXT NOT NULL DEFAULT 'healthy'
);

CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_seq ON events(seq);
CREATE INDEX IF NOT EXISTS idx_metrics_experiment ON metrics(experiment_id, source);
CREATE INDEX IF NOT EXISTS idx_leaderboard_key ON leaderboard_entries(comparison_key, score);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

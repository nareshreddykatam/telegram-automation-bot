"""Database schema migrations.

Migration 1 is intentionally a single, complete baseline. Future schema changes
must be appended as new numbered migrations; never edit an applied migration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    sql: str


BASELINE_SQL = """
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('owner', 'admin')),
    label TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    created_by INTEGER
);

CREATE TABLE IF NOT EXISTS source_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL UNIQUE,
    title TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    auto_approve INTEGER NOT NULL DEFAULT 1 CHECK (auto_approve IN (0, 1)),
    created_at TEXT NOT NULL,
    created_by INTEGER
);

CREATE TABLE IF NOT EXISTS source_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat_id INTEGER NOT NULL REFERENCES source_chats(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    media_group_id TEXT,
    content_type TEXT NOT NULL,
    has_media INTEGER NOT NULL DEFAULT 0 CHECK (has_media IN (0, 1)),
    caption TEXT,
    text_content TEXT,
    message_date TEXT,
    indexed_at TEXT NOT NULL,
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_source_messages_source
    ON source_messages(source_chat_id, id);
CREATE INDEX IF NOT EXISTS idx_source_messages_media_group
    ON source_messages(media_group_id);

CREATE TABLE IF NOT EXISTS destinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL UNIQUE,
    title TEXT,
    batch_size INTEGER NOT NULL DEFAULT 100 CHECK (batch_size > 0),
    auto_approve INTEGER NOT NULL DEFAULT 1 CHECK (auto_approve IN (0, 1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    created_at TEXT NOT NULL,
    created_by INTEGER
);

CREATE TABLE IF NOT EXISTS publishing_queues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    destination_id INTEGER NOT NULL UNIQUE
        REFERENCES destinations(id) ON DELETE CASCADE,
    next_sequence INTEGER NOT NULL DEFAULT 1 CHECK (next_sequence >= 1),
    last_success_sequence INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publishing_runs (
    id TEXT PRIMARY KEY,
    destination_id INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    planned_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    uncertain_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_publishing_runs_destination
    ON publishing_runs(destination_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_publishing_runs_status
    ON publishing_runs(status);

CREATE TABLE IF NOT EXISTS publishing_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    destination_id INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    source_message_id INTEGER NOT NULL REFERENCES source_messages(id),
    source_chat_id INTEGER NOT NULL,
    source_telegram_message_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('sending', 'sent', 'failed', 'uncertain', 'retry')
    ),
    attempts INTEGER NOT NULL DEFAULT 1,
    sent_message_id INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (destination_id, source_message_id)
);

CREATE INDEX IF NOT EXISTS idx_publishing_history_run
    ON publishing_history(run_id);
CREATE INDEX IF NOT EXISTS idx_publishing_history_destination_status
    ON publishing_history(destination_id, status);

CREATE TABLE IF NOT EXISTS moderation_settings (
    chat_id INTEGER PRIMARY KEY,
    link_protection INTEGER NOT NULL DEFAULT 0 CHECK (link_protection IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS moderation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER,
    rule TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_moderation_logs_chat
    ON moderation_logs(chat_id, created_at DESC);

CREATE TABLE IF NOT EXISTS join_request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT,
    chat_title TEXT,
    action TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_join_request_logs_chat
    ON join_request_logs(chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_join_request_logs_action
    ON join_request_logs(action, updated_at DESC);

CREATE TABLE IF NOT EXISTS scheduler_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_runs (
    schedule_date TEXT NOT NULL,
    destination_id INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    run_id TEXT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (schedule_date, destination_id)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_runs_status
    ON scheduled_runs(status, schedule_date);
"""


MIGRATIONS: tuple[Migration, ...] = (Migration(1, BASELINE_SQL),)

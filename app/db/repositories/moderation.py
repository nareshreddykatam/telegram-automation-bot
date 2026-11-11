"""Moderation configuration and audit log persistence."""

from __future__ import annotations

from app.db.database import Database
from app.db.models import ModerationSettings
from app.utils.time import utc_now_iso


async def get_settings(db: Database, chat_id: int) -> ModerationSettings | None:
    row = await db.fetchone("SELECT * FROM moderation_settings WHERE chat_id = ?", (chat_id,))
    return ModerationSettings.from_row(row) if row else None


async def upsert_settings(db: Database, chat_id: int, enabled: bool) -> ModerationSettings:
    now = utc_now_iso()
    await db.execute(
        """
        INSERT INTO moderation_settings(chat_id, link_protection, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            link_protection = excluded.link_protection,
            updated_at = excluded.updated_at
        """,
        (chat_id, 1 if enabled else 0, now, now),
    )
    settings = await get_settings(db, chat_id)
    if settings is None:
        raise RuntimeError("Moderation settings were not persisted")
    return settings


async def is_link_protection_enabled(db: Database, chat_id: int) -> bool:
    row = await db.fetchone(
        "SELECT link_protection FROM moderation_settings WHERE chat_id = ?",
        (chat_id,),
    )
    return bool(row["link_protection"]) if row else False


async def log_action(
    db: Database,
    *,
    chat_id: int,
    message_id: int,
    user_id: int | None,
    rule: str,
    action: str,
    status: str,
    detail: str | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO moderation_logs(
            chat_id, message_id, user_id, rule, action, status, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            message_id,
            user_id,
            rule,
            action,
            status,
            detail,
            utc_now_iso(),
        ),
    )


async def recent_logs(db: Database, chat_id: int | None = None, limit: int = 10):
    if chat_id is None:
        return await db.fetchall("SELECT * FROM moderation_logs ORDER BY id DESC LIMIT ?", (limit,))
    return await db.fetchall(
        "SELECT * FROM moderation_logs WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    )

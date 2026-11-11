"""Source chat persistence."""

from __future__ import annotations

from app.db.database import Database
from app.db.models import SourceChat
from app.utils.time import utc_now_iso


async def add_source(
    db: Database,
    chat_id: int,
    title: str | None,
    created_by: int,
    auto_approve: bool = True,
) -> SourceChat:
    now = utc_now_iso()
    await db.execute(
        """
        INSERT INTO source_chats(chat_id, title, enabled, auto_approve, created_at, created_by)
        VALUES (?, ?, 1, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title = COALESCE(excluded.title, source_chats.title),
            enabled = 1,
            auto_approve = excluded.auto_approve
        """,
        (chat_id, title, 1 if auto_approve else 0, now, created_by),
    )
    source = await get_by_chat_id(db, chat_id)
    if source is None:
        raise RuntimeError("Source chat was not persisted")
    return source


async def get_by_chat_id(db: Database, chat_id: int) -> SourceChat | None:
    row = await db.fetchone("SELECT * FROM source_chats WHERE chat_id = ?", (chat_id,))
    return SourceChat.from_row(row) if row else None


async def list_sources(db: Database, enabled_only: bool = True) -> list[SourceChat]:
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = await db.fetchall(f"SELECT * FROM source_chats {where} ORDER BY id")
    return [SourceChat.from_row(row) for row in rows]


async def set_enabled(db: Database, chat_id: int, enabled: bool) -> bool:
    cursor = await db.execute(
        "UPDATE source_chats SET enabled = ? WHERE chat_id = ?",
        (1 if enabled else 0, chat_id),
    )
    return cursor.rowcount > 0


async def set_auto_approve(db: Database, chat_id: int, enabled: bool) -> bool:
    cursor = await db.execute(
        "UPDATE source_chats SET auto_approve = ? WHERE chat_id = ?",
        (1 if enabled else 0, chat_id),
    )
    return cursor.rowcount > 0


async def message_count(db: Database, source_chat_id: int) -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) AS count FROM source_messages WHERE source_chat_id = ?",
        (source_chat_id,),
    )
    return int(row["count"]) if row else 0

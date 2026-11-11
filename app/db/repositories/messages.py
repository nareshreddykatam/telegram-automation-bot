"""Source message indexing."""

from __future__ import annotations

from app.db.database import Database
from app.db.models import SourceMessage
from app.utils.time import utc_now_iso


async def index_message(
    db: Database,
    *,
    source_chat_id: int,
    chat_id: int,
    message_id: int,
    content_type: str,
    has_media: bool,
    caption: str | None,
    text_content: str | None,
    media_group_id: str | None,
    message_date: str | None,
) -> tuple[SourceMessage, bool]:
    """Insert a source message once.

    Returns (record, created). Telegram may deliver the same update twice after
    network errors, so this is idempotent on (chat_id, message_id).
    """

    cursor = await db.execute(
        """
        INSERT OR IGNORE INTO source_messages(
            source_chat_id, chat_id, message_id, media_group_id, content_type,
            has_media, caption, text_content, message_date, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_chat_id,
            chat_id,
            message_id,
            media_group_id,
            content_type,
            1 if has_media else 0,
            caption,
            text_content,
            message_date,
            utc_now_iso(),
        ),
    )
    created = cursor.rowcount > 0
    row = await db.fetchone(
        "SELECT * FROM source_messages WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )
    if row is None:
        raise RuntimeError("Source message was not persisted")
    return SourceMessage.from_row(row), created


async def get_by_id(db: Database, message_pk: int) -> SourceMessage | None:
    row = await db.fetchone("SELECT * FROM source_messages WHERE id = ?", (message_pk,))
    return SourceMessage.from_row(row) if row else None


async def max_sequence(db: Database) -> int:
    row = await db.fetchone("SELECT COALESCE(MAX(id), 0) AS max_id FROM source_messages")
    return int(row["max_id"]) if row else 0


async def count_from(db: Database, sequence: int) -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) AS count FROM source_messages WHERE id >= ?", (sequence,)
    )
    return int(row["count"]) if row else 0


async def count_all(db: Database) -> int:
    row = await db.fetchone("SELECT COUNT(*) AS count FROM source_messages")
    return int(row["count"]) if row else 0

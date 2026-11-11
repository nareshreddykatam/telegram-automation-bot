"""Indexing source channel/group messages without downloading media."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.db.database import Database
from app.db.models import SourceChat, SourceMessage
from app.db.repositories import messages as messages_repo
from app.utils.time import iso_utc

logger = logging.getLogger(__name__)

SUPPORTED_CONTENT_TYPES = frozenset(
    {
        "text",
        "photo",
        "video",
        "document",
        "audio",
        "voice",
        "animation",
        "video_note",
        "sticker",
    }
)


class SourceIngestService:
    def __init__(self, db: Database) -> None:
        self._db = db
        # Serialize inserts so concurrent updates cannot interleave album items
        # and break the global upload-order sequence.
        self._lock = asyncio.Lock()

    async def index(self, message: Any, source: SourceChat) -> SourceMessage | None:
        async with self._lock:
            return await self._index_locked(message, source)

    async def _index_locked(self, message: Any, source: SourceChat) -> SourceMessage | None:
        content_type = str(getattr(message, "content_type", "text"))
        if content_type not in SUPPORTED_CONTENT_TYPES:
            logger.info(
                "Skipping unsupported source content type %s in %s",
                content_type,
                source.chat_id,
            )
            return None

        text = getattr(message, "text", None)
        caption = getattr(message, "caption", None)
        media_group_id = getattr(message, "media_group_id", None)
        message_date = getattr(message, "date", None)
        if isinstance(message_date, datetime):
            message_date_value = iso_utc(message_date)
        else:
            message_date_value = str(message_date) if message_date else None

        record, created = await messages_repo.index_message(
            self._db,
            source_chat_id=source.id,
            chat_id=int(message.chat.id),
            message_id=int(message.message_id),
            content_type=content_type,
            has_media=content_type != "text",
            caption=caption,
            text_content=text,
            media_group_id=str(media_group_id) if media_group_id else None,
            message_date=message_date_value,
        )
        if created:
            logger.info(
                "Indexed source message chat=%s message=%s type=%s sequence=%s album=%s",
                record.chat_id,
                record.message_id,
                record.content_type,
                record.id,
                record.media_group_id,
            )
        return record

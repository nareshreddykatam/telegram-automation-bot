"""Reusable aiogram filters backed by database configuration."""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import Message

from app.db.database import Database
from app.db.repositories import moderation as moderation_repo
from app.db.repositories import sources as sources_repo


class SourceChatFilter(BaseFilter):
    """Match non-command messages in enabled source chats."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(self, message: Message, **kwargs: Any) -> bool | dict[str, Any]:
        if message.chat is None:
            return False
        source = await sources_repo.get_by_chat_id(self._db, int(message.chat.id))
        if source is None or not source.enabled:
            return False

        user = getattr(message, "from_user", None)
        if user is not None and getattr(user, "is_bot", False):
            return False

        text = getattr(message, "text", None)
        if text and text.lstrip().startswith("/"):
            return False
        for entity in getattr(message, "entities", None) or ():
            entity_type = getattr(entity, "type", None)
            if getattr(entity_type, "value", entity_type) == "bot_command":
                return False
        return {"source": source}


class LinkProtectionFilter(BaseFilter):
    """Match group messages in chats where link protection is enabled."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(self, message: Message, **kwargs: Any) -> bool:
        if message.chat is None or message.chat.type not in {"group", "supergroup"}:
            return False
        return await moderation_repo.is_link_protection_enabled(self._db, int(message.chat.id))

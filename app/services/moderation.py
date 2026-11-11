"""Group moderation: remove links posted by non-administrators."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.constants import TELEGRAM_ANONYMOUS_ADMIN_ID
from app.db.database import Database
from app.db.repositories import moderation as moderation_repo
from app.utils.links import detect_links

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModerationOutcome:
    handled: bool
    deleted: bool
    reason: str
    links: tuple[str, ...] = ()
    error: str | None = None


class ModerationService:
    def __init__(
        self,
        db: Database,
        bot: Any,
        *,
        admin_cache_ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._bot = bot
        self._ttl = admin_cache_ttl
        self._clock = clock
        self._admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}

    async def is_admin(self, chat_id: int, user_id: int) -> bool:
        key = (chat_id, user_id)
        cached = self._admin_cache.get(key)
        now = self._clock()
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        member = await self._bot.get_chat_member(chat_id, user_id)
        status = getattr(member, "status", None)
        value = getattr(status, "value", status)
        is_admin = value in {"creator", "administrator"}
        self._admin_cache[key] = (now, is_admin)
        return is_admin

    async def process_message(self, message: Any) -> ModerationOutcome | None:
        chat = getattr(message, "chat", None)
        if chat is None or getattr(chat, "type", None) not in {"group", "supergroup"}:
            return None

        chat_id = int(chat.id)
        if not await moderation_repo.is_link_protection_enabled(self._db, chat_id):
            return None

        text = getattr(message, "text", None)
        entities = getattr(message, "entities", None)
        if not text:
            text = getattr(message, "caption", None)
            entities = getattr(message, "caption_entities", None)

        # Never treat bot commands themselves as spam.
        if text and text.lstrip().startswith("/"):
            return None
        for entity in entities or ():
            entity_type = getattr(entity, "type", None)
            value = getattr(entity_type, "value", entity_type)
            if value == "bot_command":
                return None

        links = detect_links(text, entities)
        if not links:
            return None

        user = getattr(message, "from_user", None)
        sender_chat = getattr(message, "sender_chat", None)
        message_id = int(getattr(message, "message_id", 0))
        user_id = int(getattr(user, "id", 0)) or None

        if user is None or sender_chat is not None:
            return ModerationOutcome(
                handled=True, deleted=False, reason="no_human_sender", links=tuple(links)
            )
        if getattr(user, "is_bot", False):
            return ModerationOutcome(
                handled=True, deleted=False, reason="bot_sender", links=tuple(links)
            )
        if int(user.id) == TELEGRAM_ANONYMOUS_ADMIN_ID:
            return ModerationOutcome(
                handled=True, deleted=False, reason="anonymous_admin", links=tuple(links)
            )

        try:
            admin = await self.is_admin(chat_id, int(user.id))
        except Exception as exc:  # Permission lookup failed: never delete on doubt.
            logger.warning("Could not verify admin status in %s: %s", chat_id, exc)
            await moderation_repo.log_action(
                self._db,
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
                rule="link_protection",
                action="delete",
                status="error",
                detail=f"Admin verification failed: {exc}",
            )
            return ModerationOutcome(
                handled=True,
                deleted=False,
                reason="admin_check_failed",
                links=tuple(links),
                error=str(exc),
            )

        if admin:
            return ModerationOutcome(
                handled=True, deleted=False, reason="administrator", links=tuple(links)
            )

        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            logger.warning("Failed to delete link message %s in %s: %s", message_id, chat_id, exc)
            await moderation_repo.log_action(
                self._db,
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
                rule="link_protection",
                action="delete",
                status="failed",
                detail=str(exc),
            )
            return ModerationOutcome(
                handled=True,
                deleted=False,
                reason="delete_failed",
                links=tuple(links),
                error=str(exc),
            )

        await moderation_repo.log_action(
            self._db,
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            rule="link_protection",
            action="delete",
            status="success",
            detail=", ".join(links)[:500],
        )
        logger.info("Deleted link message %s from user %s in chat %s", message_id, user_id, chat_id)
        return ModerationOutcome(
            handled=True, deleted=True, reason="link_deleted", links=tuple(links)
        )

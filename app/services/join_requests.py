"""Automatic join request approval and retry support."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.constants import JOIN_APPROVED, JOIN_FAILED, JOIN_IGNORED
from app.db.database import Database
from app.db.repositories import destinations as destinations_repo
from app.db.repositories import join_requests as join_repo
from app.db.repositories import sources as sources_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    status: str
    chat_id: int
    user_id: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RetrySummary:
    approved: int
    failed: int
    attempted: int


class JoinRequestService:
    def __init__(self, db: Database, bot: Any) -> None:
        self._db = db
        self._bot = bot

    async def _configured_auto_approve(self, chat_id: int) -> tuple[bool, str | None]:
        destination = await destinations_repo.get_by_chat_id(self._db, chat_id)
        if destination is not None:
            return destination.auto_approve and destination.enabled, destination.title
        source = await sources_repo.get_by_chat_id(self._db, chat_id)
        if source is not None:
            return source.auto_approve and source.enabled, source.title
        return False, None

    async def handle_request(self, request: Any) -> ApprovalResult | None:
        chat = getattr(request, "chat", None)
        user = getattr(request, "from_user", None)
        if chat is None or user is None:
            return None
        chat_id = int(chat.id)
        user_id = int(user.id)
        chat_title = getattr(chat, "title", None)

        configured, _ = await self._configured_auto_approve(chat_id)
        destination = await destinations_repo.get_by_chat_id(self._db, chat_id)
        source = await sources_repo.get_by_chat_id(self._db, chat_id)
        if destination is None and source is None:
            # This bot can be an admin in unrelated chats; do not build an
            # unbounded log of unrelated requests.
            return None

        if not configured:
            await join_repo.log_request(
                self._db,
                chat_id=chat_id,
                user_id=user_id,
                username=getattr(user, "username", None),
                first_name=getattr(user, "first_name", None),
                chat_title=chat_title,
                action=JOIN_IGNORED,
                error="Auto-approval disabled or chat disabled",
            )
            return ApprovalResult(JOIN_IGNORED, chat_id, user_id)

        log_id = await join_repo.log_request(
            self._db,
            chat_id=chat_id,
            user_id=user_id,
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
            chat_title=chat_title,
            action=JOIN_FAILED,  # optimistic; updated on success
        )
        try:
            await self._bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception as exc:
            logger.warning(
                "Join request approval failed for user %s in %s: %s",
                user_id,
                chat_id,
                exc,
            )
            await join_repo.update_result(
                self._db, log_id=log_id, action=JOIN_FAILED, error=str(exc)
            )
            return ApprovalResult(JOIN_FAILED, chat_id, user_id, str(exc))

        await join_repo.update_result(self._db, log_id=log_id, action=JOIN_APPROVED, error=None)
        logger.info("Approved join request for user %s in %s", user_id, chat_id)
        return ApprovalResult(JOIN_APPROVED, chat_id, user_id)

    async def approve_pending(
        self, *, chat_id: int | None = None, limit: int = 200
    ) -> RetrySummary:
        """Retry previously logged failed/ignored requests.

        Telegram does not expose a method to list all pending join requests.
        This command can only retry requests the bot observed and logged.
        """

        rows = await join_repo.recent_pending_or_failed(self._db, chat_id=chat_id, limit=limit)
        approved = 0
        failed = 0
        for row in rows:
            try:
                await self._bot.approve_chat_join_request(chat_id=row.chat_id, user_id=row.user_id)
            except Exception as exc:
                failed += 1
                await join_repo.update_result(
                    self._db,
                    log_id=row.id,
                    action=JOIN_FAILED,
                    error=str(exc),
                    increment_attempts=True,
                )
                logger.warning(
                    "Retry join approval failed for user %s in %s: %s",
                    row.user_id,
                    row.chat_id,
                    exc,
                )
            else:
                approved += 1
                await join_repo.update_result(
                    self._db,
                    log_id=row.id,
                    action=JOIN_APPROVED,
                    error=None,
                    increment_attempts=True,
                )
        return RetrySummary(approved=approved, failed=failed, attempted=len(rows))

"""Only explicitly configured administrators may use admin commands."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.db.database import Database
from app.db.repositories import admins as admins_repo

logger = logging.getLogger(__name__)


class AdminGuardMiddleware(BaseMiddleware):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None

        if not await admins_repo.is_admin(self._db, int(user.id)):
            logger.warning(
                "Unauthorized admin command attempt by user %s (%s)",
                user.id,
                getattr(user, "username", None),
            )
            if isinstance(event, Message):
                await event.answer(
                    f"Not authorized. Your Telegram ID is {user.id}. Ask the bot owner to add it."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("Not authorized.", show_alert=True)
            return None

        data["is_admin"] = True
        return await handler(event, data)

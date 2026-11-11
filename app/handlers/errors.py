"""Global error handler: logs unexpected errors and reports expected ones."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import ErrorEvent, Message

from app.utils.errors import CommandError

logger = logging.getLogger(__name__)

router = Router(name="errors")


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    update = event.update
    message: Message | None = update.message or update.channel_post
    if isinstance(exc, CommandError):
        if message is not None:
            await message.answer(str(exc))
        return True

    logger.error(
        "Unhandled update error: %s",
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    if message is not None:
        try:
            await message.answer("An internal error occurred. The owner can check the logs.")
        except Exception:
            pass
    return True

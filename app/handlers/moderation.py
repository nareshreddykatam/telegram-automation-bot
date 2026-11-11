"""Group message moderation handler."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message

from app.services.moderation import ModerationService

logger = logging.getLogger(__name__)

router = Router(name="moderation")


@router.message()
async def moderate_group_message(message: Message, moderation: ModerationService) -> None:
    try:
        outcome = await moderation.process_message(message)
    except Exception:
        logger.exception("Moderation handler failed")
        return
    if outcome and outcome.deleted:
        logger.info(
            "Moderation deleted message %s in chat %s",
            message.message_id,
            message.chat.id,
        )

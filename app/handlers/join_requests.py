"""Chat join request handler."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import ChatJoinRequest

from app.services.join_requests import JoinRequestService

logger = logging.getLogger(__name__)

router = Router(name="join_requests")


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, join_service: JoinRequestService) -> None:
    try:
        result = await join_service.handle_request(request)
    except Exception:
        logger.exception(
            "Unhandled join request error chat=%s user=%s",
            getattr(getattr(request, "chat", None), "id", None),
            getattr(getattr(request, "from_user", None), "id", None),
        )
        return
    if result is not None:
        logger.info(
            "Join request chat=%s user=%s result=%s",
            result.chat_id,
            result.user_id,
            result.status,
        )

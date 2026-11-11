"""Source channel/group indexing handlers."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message

from app.db.models import SourceChat
from app.services.ingest import SourceIngestService
from app.services.moderation import ModerationService

logger = logging.getLogger(__name__)

router = Router(name="source")


@router.channel_post()
async def on_channel_post(
    message: Message,
    ingest: SourceIngestService,
    source: SourceChat,
    moderation: ModerationService,
) -> None:
    await ingest.index(message, source)
    await moderation.process_message(message)


@router.message()
async def on_group_source_message(
    message: Message,
    ingest: SourceIngestService,
    source: SourceChat,
    moderation: ModerationService,
) -> None:
    await ingest.index(message, source)
    await moderation.process_message(message)

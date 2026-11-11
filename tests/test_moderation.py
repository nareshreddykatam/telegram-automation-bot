from __future__ import annotations

import pytest

from app.db.database import Database
from app.db.repositories import moderation as moderation_repo
from app.services.moderation import ModerationService
from tests.helpers import (
    DEST_CHAT_ID,
    FakeBot,
    FakeChat,
    FakeMessage,
    FakeUser,
    create_destination,
)


@pytest.mark.asyncio
async def test_non_admin_link_is_deleted(db: Database):
    await create_destination(db, chat_id=DEST_CHAT_ID)
    await moderation_repo.upsert_settings(db, DEST_CHAT_ID, True)
    bot = FakeBot()
    service = ModerationService(db, bot)
    message = FakeMessage(
        chat=FakeChat(DEST_CHAT_ID),
        message_id=10,
        from_user=FakeUser(42),
        text="join https://spam.example",
    )
    outcome = await service.process_message(message)
    assert outcome is not None
    assert outcome.deleted is True
    assert bot.deleted == [(DEST_CHAT_ID, 10)]
    log = await db.fetchone("SELECT * FROM moderation_logs ORDER BY id DESC LIMIT 1")
    assert log["status"] == "success"
    assert log["user_id"] == 42


@pytest.mark.asyncio
async def test_administrator_links_are_never_deleted(db: Database):
    await create_destination(db, chat_id=DEST_CHAT_ID)
    await moderation_repo.upsert_settings(db, DEST_CHAT_ID, True)
    bot = FakeBot()
    bot.member_status[(DEST_CHAT_ID, 77)] = "creator"
    service = ModerationService(db, bot)
    message = FakeMessage(
        chat=FakeChat(DEST_CHAT_ID),
        message_id=11,
        from_user=FakeUser(77),
        text="official link https://example.com",
    )
    outcome = await service.process_message(message)
    assert outcome is not None
    assert outcome.deleted is False
    assert outcome.reason == "administrator"
    assert bot.deleted == []


@pytest.mark.asyncio
async def test_bot_messages_and_disabled_protection_are_ignored(db: Database):
    await create_destination(db, chat_id=DEST_CHAT_ID)
    bot = FakeBot()
    service = ModerationService(db, bot)
    message = FakeMessage(
        chat=FakeChat(DEST_CHAT_ID),
        message_id=12,
        from_user=FakeUser(42),
        text="https://example.com",
    )
    # Protection is disabled by default for a newly added destination.
    assert await service.process_message(message) is None

    await moderation_repo.upsert_settings(db, DEST_CHAT_ID, True)
    bot_message = FakeMessage(
        chat=FakeChat(DEST_CHAT_ID),
        message_id=13,
        from_user=FakeUser(42, is_bot=True),
        text="https://example.com",
    )
    outcome = await service.process_message(bot_message)
    assert outcome is not None
    assert outcome.deleted is False
    assert outcome.reason == "bot_sender"
    assert bot.deleted == []


@pytest.mark.asyncio
async def test_message_without_links_is_not_moderated(db: Database):
    await create_destination(db, chat_id=DEST_CHAT_ID)
    await moderation_repo.upsert_settings(db, DEST_CHAT_ID, True)
    bot = FakeBot()
    service = ModerationService(db, bot)
    message = FakeMessage(
        chat=FakeChat(DEST_CHAT_ID),
        message_id=14,
        from_user=FakeUser(42),
        text="just a normal message",
    )
    assert await service.process_message(message) is None
    assert bot.deleted == []

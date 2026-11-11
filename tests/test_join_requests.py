from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.database import Database
from app.db.repositories import destinations as destinations_repo
from app.services.join_requests import JoinRequestService
from tests.helpers import DEST_CHAT_ID, FakeBot, FakeChat, FakeUser, create_destination


@pytest.mark.asyncio
async def test_join_request_is_approved_and_logged(db: Database):
    await create_destination(db, chat_id=DEST_CHAT_ID)
    bot = FakeBot()
    service = JoinRequestService(db, bot)
    request = SimpleNamespace(
        chat=FakeChat(DEST_CHAT_ID, title="Destination"),
        from_user=FakeUser(500, username="newbie", first_name="New"),
    )
    result = await service.handle_request(request)
    assert result is not None
    assert result.status == "approved"
    assert bot.approvals == [(DEST_CHAT_ID, 500)]
    log = await db.fetchone("SELECT * FROM join_request_logs ORDER BY id DESC LIMIT 1")
    assert log["action"] == "approved"
    assert log["user_id"] == 500


@pytest.mark.asyncio
async def test_join_approval_failure_is_logged_and_retryable(db: Database):
    await create_destination(db, chat_id=DEST_CHAT_ID)
    bot = FakeBot()
    bot.approve_failures = 1
    service = JoinRequestService(db, bot)
    request = SimpleNamespace(
        chat=FakeChat(DEST_CHAT_ID, title="Destination"),
        from_user=FakeUser(501),
    )
    first = await service.handle_request(request)
    assert first is not None
    assert first.status == "failed"
    log = await db.fetchone("SELECT * FROM join_request_logs ORDER BY id DESC LIMIT 1")
    assert log["action"] == "failed"
    assert log["attempts"] == 1

    summary = await service.approve_pending(chat_id=DEST_CHAT_ID)
    assert summary.attempted == 1
    assert summary.approved == 1
    assert summary.failed == 0
    updated = await db.fetchone("SELECT * FROM join_request_logs WHERE id = ?", (log["id"],))
    assert updated["action"] == "approved"
    assert updated["attempts"] == 2


@pytest.mark.asyncio
async def test_autoapprove_off_logs_ignored_without_approving(db: Database):
    destination = await create_destination(db, chat_id=DEST_CHAT_ID)
    await destinations_repo.set_auto_approve(db, destination.chat_id, False)
    bot = FakeBot()
    service = JoinRequestService(db, bot)
    request = SimpleNamespace(
        chat=FakeChat(DEST_CHAT_ID, title="Destination"),
        from_user=FakeUser(502),
    )
    result = await service.handle_request(request)
    assert result is not None
    assert result.status == "ignored"
    assert bot.approvals == []
    log = await db.fetchone("SELECT * FROM join_request_logs ORDER BY id DESC LIMIT 1")
    assert log["action"] == "ignored"


@pytest.mark.asyncio
async def test_unconfigured_chat_request_is_not_logged(db: Database):
    bot = FakeBot()
    service = JoinRequestService(db, bot)
    request = SimpleNamespace(
        chat=FakeChat(-1009999999999, title="Unrelated"),
        from_user=FakeUser(503),
    )
    result = await service.handle_request(request)
    assert result is None
    count = await db.fetchone("SELECT COUNT(*) AS count FROM join_request_logs")
    assert count["count"] == 0

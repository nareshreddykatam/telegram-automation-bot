from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db.database import Database
from app.db.repositories import sources as sources_repo
from app.services.ingest import SourceIngestService
from tests.helpers import FakeChat, FakeMessage, FakeUser


@pytest.mark.asyncio
async def test_ingest_preserves_caption_album_and_media_metadata(db: Database):
    source = await sources_repo.add_source(
        db, chat_id=-100777, title="Private source", created_by=1
    )
    message = FakeMessage(
        chat=FakeChat(-100777, type="channel", title="Private source"),
        message_id=20,
        from_user=FakeUser(1),
        caption="Original caption",
        content_type="photo",
        media_group_id="12345",
        date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )
    service = SourceIngestService(db)
    record = await service.index(message, source)

    assert record is not None
    assert record.caption == "Original caption"
    assert record.has_media is True
    assert record.media_group_id == "12345"
    assert record.message_date.startswith("2026-01-01T12:00:00")
    assert record.message_id == 20
    assert record.chat_id == -100777

    duplicate = await service.index(message, source)
    assert duplicate is not None
    assert duplicate.id == record.id
    count = await db.fetchone("SELECT COUNT(*) AS count FROM source_messages")
    assert count["count"] == 1


@pytest.mark.asyncio
async def test_unsupported_content_is_not_indexed(db: Database):
    source = await sources_repo.add_source(db, chat_id=-100778, title="Source", created_by=1)
    message = FakeMessage(
        chat=FakeChat(-100778, type="channel"),
        message_id=21,
        content_type="poll",
    )
    service = SourceIngestService(db)
    assert await service.index(message, source) is None
    count = await db.fetchone("SELECT COUNT(*) AS count FROM source_messages")
    assert count["count"] == 0

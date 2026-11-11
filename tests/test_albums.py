from __future__ import annotations

import pytest

from app.config import Settings
from app.constants import RUN_COMPLETED
from app.db.database import Database
from app.services.publishing import PublishingService
from tests.helpers import FakeSender, create_destination, create_source


@pytest.mark.asyncio
async def test_media_groups_are_copied_as_albums_and_order_is_preserved(
    db: Database, settings: Settings
):
    await create_source(db, count=12, album_every=4, album_size=3)
    destination = await create_destination(db, batch_size=20)
    sender = FakeSender()
    service = PublishingService(db, sender, settings)

    result = await service.publish_destination(destination.id)
    assert result.status == RUN_COMPLETED
    assert result.success_count == 12
    assert result.next_sequence == 13

    assert len(sender.albums) == 3
    assert [album[2] for album in sender.albums] == [
        (1, 2, 3),
        (5, 6, 7),
        (9, 10, 11),
    ]
    # Individual messages and album items all remain in source order.
    assert [item[2] for item in sender.sent] == list(range(1, 13))


@pytest.mark.asyncio
async def test_album_is_not_split_by_batch_boundary(db: Database, settings: Settings):
    await create_source(db, count=12, album_every=4, album_size=3)
    destination = await create_destination(db, batch_size=5)
    sender = FakeSender()
    service = PublishingService(db, sender, settings)

    first = await service.publish_destination(destination.id)
    # Album 1-3 fits, 4 fits, album 5-7 would exceed the batch, so it is deferred.
    assert first.success_count == 4
    assert first.next_sequence == 5
    assert sender.albums == [(destination.chat_id, -1001111111111, (1, 2, 3))]

    second = await service.publish_destination(destination.id)
    assert second.success_count == 4
    assert second.next_sequence == 9
    assert [album[2] for album in sender.albums] == [
        (1, 2, 3),
        (5, 6, 7),
    ]

    third = await service.publish_destination(destination.id)
    assert third.success_count == 4
    assert third.next_sequence == 13

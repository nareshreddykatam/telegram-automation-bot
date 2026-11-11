from __future__ import annotations

import pytest

from app.config import Settings
from app.db.database import Database
from app.services.publishing import PublishingService
from tests.helpers import FakeSender, create_destination, create_source


@pytest.mark.asyncio
async def test_flood_wait_is_retried(db: Database, settings: Settings):
    await create_source(db, count=3)
    destination = await create_destination(db, batch_size=10)
    sender = FakeSender()
    sender.flood_failures[2] = 1
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = PublishingService(db, sender, settings, sleep=fake_sleep)
    result = await service.publish_destination(destination.id)

    assert result.success_count == 3
    assert [item[2] for item in sender.sent] == [1, 2, 3]
    assert sleeps and sleeps[0] == 1


@pytest.mark.asyncio
async def test_transient_errors_are_retried_before_batch_abort(db: Database, settings: Settings):
    await create_source(db, count=3)
    destination = await create_destination(db, batch_size=10)
    sender = FakeSender()
    sender.transient_failures[2] = 2
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = PublishingService(db, sender, settings, sleep=fake_sleep)
    result = await service.publish_destination(destination.id)

    assert result.success_count == 3
    assert [item[2] for item in sender.sent] == [1, 2, 3]
    assert sleeps == []  # backoff is configured to 0 in test settings

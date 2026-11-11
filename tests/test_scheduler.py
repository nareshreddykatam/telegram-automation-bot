from __future__ import annotations

from datetime import date

import pytest

from app.config import Settings
from app.db.database import Database
from app.db.repositories import scheduler as scheduler_repo
from app.scheduler.scheduler import PublishingScheduler
from app.services.publishing import PublishingService
from app.services.reports import ReportService
from tests.helpers import FakeBot, FakeSender, create_destination, create_source


def build_scheduler(db: Database, settings: Settings, sender: FakeSender):
    publishing = PublishingService(db, sender, settings)
    reports = ReportService(db, FakeBot(), settings)
    return PublishingScheduler(db, settings, publishing, reports), publishing


@pytest.mark.asyncio
async def test_scheduled_date_is_processed_once(db: Database, settings: Settings):
    await create_source(db, count=250)
    await create_destination(db, batch_size=100)
    sender = FakeSender()
    scheduler, _ = build_scheduler(db, settings, sender)

    first = await scheduler.run_cycle("2026-01-01", "schedule")
    assert len(first) == 1
    assert first[0].success_count == 100

    second = await scheduler.run_cycle("2026-01-01", "schedule")
    assert second == []
    assert len(sender.sent) == 100

    third = await scheduler.run_cycle("2026-01-02", "schedule")
    assert len(third) == 1
    assert third[0].success_count == 100
    assert len(sender.sent) == 200

    last_cycle = await scheduler_repo.get_last_cycle_date(db)
    assert last_cycle == "2026-01-02"


@pytest.mark.asyncio
async def test_global_pause_prevents_scheduled_cycle(db: Database, settings: Settings):
    await create_source(db, count=10)
    await create_destination(db, batch_size=10)
    sender = FakeSender()
    scheduler, _ = build_scheduler(db, settings, sender)

    await scheduler_repo.set_globally_paused(db, True)
    results = await scheduler.run_cycle("2026-01-01", "schedule")
    assert results == []
    assert sender.sent == []

    await scheduler_repo.set_globally_paused(db, False)
    results = await scheduler.run_cycle("2026-01-01", "schedule")
    assert len(results) == 1
    assert results[0].success_count == 10
    assert len(sender.sent) == 10


@pytest.mark.asyncio
async def test_catchup_processes_the_most_recent_missed_date(
    db: Database, settings: Settings, monkeypatch
):
    await create_source(db, count=250)
    await create_destination(db, batch_size=100)
    sender = FakeSender()
    scheduler, _ = build_scheduler(db, settings, sender)

    await scheduler_repo.set_last_cycle_date(db, "2026-01-01")
    monkeypatch.setattr(
        "app.scheduler.scheduler.scheduled_target_date",
        lambda *args, **kwargs: date(2026, 1, 3),
    )
    results = await scheduler.catch_up_if_needed("startup")
    assert len(results) == 2
    assert [result.success_count for result in results] == [100, 100]
    last_cycle = await scheduler_repo.get_last_cycle_date(db)
    assert last_cycle == "2026-01-03"

    # A second startup after the catch-up must not publish another batch.
    await scheduler.catch_up_if_needed("startup")
    assert len(sender.sent) == 200


@pytest.mark.asyncio
async def test_first_startup_without_cycle_history_does_not_auto_publish(
    db: Database, settings: Settings, monkeypatch
):
    await create_source(db, count=50)
    await create_destination(db, batch_size=10)
    sender = FakeSender()
    scheduler, _ = build_scheduler(db, settings, sender)

    monkeypatch.setattr(
        "app.scheduler.scheduler.scheduled_target_date",
        lambda *args, **kwargs: date(2026, 1, 3),
    )

    # Brand-new installation: nothing should be published automatically just
    # because the bot was started.
    results = await scheduler.catch_up_if_needed("startup")
    assert results == []
    assert sender.sent == []
    assert await scheduler_repo.get_last_cycle_date(db) is None

    # An explicit resume/manual catch-up still publishes the current batch.
    results = await scheduler.catch_up_if_needed("resume")
    assert len(results) == 1
    assert results[0].success_count == 10
    assert len(sender.sent) == 10
    assert await scheduler_repo.get_last_cycle_date(db) == "2026-01-03"

from __future__ import annotations

import pytest

from app.config import Settings
from app.constants import MSG_SENT, RUN_COMPLETED
from app.db.database import Database
from app.db.repositories import destinations as destinations_repo
from app.db.repositories import publishing as publishing_repo
from app.services.publishing import PublishingService
from app.services.recovery import RecoveryService
from tests.helpers import FakeSender, create_destination, create_source


@pytest.mark.asyncio
async def test_restart_recovery_marks_in_flight_message_uncertain_without_resend(
    db: Database, settings: Settings
):
    await create_source(db, count=5)
    destination = await create_destination(db, batch_size=10)
    sender = FakeSender()

    # Simulate: run reserved source #1, sent it, then crashed before recording.
    await publishing_repo.create_run(
        db,
        run_id="crashed-run",
        destination_id=destination.id,
        trigger="manual",
        status="running",
        planned_count=5,
    )
    source_message = await publishing_repo.list_candidates(
        db, destination_id=destination.id, next_sequence=1, limit=1
    )
    outcome, history_id = await publishing_repo.reserve_for_send(
        db,
        destination_id=destination.id,
        source=source_message[0].source,
        run_id="crashed-run",
    )
    assert outcome == "booked"
    assert history_id is not None

    recovery = await RecoveryService(db).recover()
    assert recovery.uncertain_messages == 1
    assert recovery.aborted_runs == 1

    queue = await destinations_repo.get_queue(db, destination.id)
    assert queue.next_sequence == 2
    row = await db.fetchone("SELECT status FROM publishing_history WHERE id = ?", (history_id,))
    assert row["status"] == "uncertain"

    service = PublishingService(db, sender, settings)
    result = await service.publish_destination(destination.id)
    assert result.status == RUN_COMPLETED
    assert result.success_count == 4
    assert [item[2] for item in sender.sent] == [2, 3, 4, 5]
    assert 1 not in [item[2] for item in sender.sent]


@pytest.mark.asyncio
async def test_manual_cursor_rewind_does_not_duplicate_sent_messages(
    db: Database, settings: Settings
):
    await create_source(db, count=10)
    destination = await create_destination(db, batch_size=100)
    sender = FakeSender()
    service = PublishingService(db, sender, settings)

    first = await service.publish_destination(destination.id)
    assert first.success_count == 10
    assert len(sender.sent) == 10

    # Simulate an operator resetting the cursor. Existing history must prevent
    # duplicate copies; the engine advances past each duplicate.
    await db.execute(
        "UPDATE publishing_queues SET next_sequence = 1 WHERE destination_id = ?",
        (destination.id,),
    )
    second = await service.publish_destination(destination.id)
    assert second.success_count == 0
    assert second.skipped_count == 10
    assert len(sender.sent) == 10

    queue = await destinations_repo.get_queue(db, destination.id)
    assert queue.next_sequence == 11
    count = await db.fetchone(
        "SELECT COUNT(*) AS count FROM publishing_history WHERE destination_id = ? AND status = ?",
        (destination.id, MSG_SENT),
    )
    assert count["count"] == 10

"""Phase 2 PostgreSQL regression tests.

These tests exercise the PostgreSQL-only request/job queue, lease and heartbeat
rules, recovery, global pacing, billing accounting and the bounded worker.
SQLite keeps its Phase 1 inline engine; the guard test at the end proves the
worker refuses it unless the explicit test-only override is used.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import Settings
from app.constants import (
    MSG_SENDING,
    MSG_SENT,
    MSG_UNCERTAIN,
    RUN_COMPLETED_WITH_ERRORS,
    RUN_INCOMPLETE,
    RUN_NO_MESSAGES,
    RUN_RUNNING,
    SCHED_RUNNING,
)
from app.db.models import SourceMessage
from app.db.repositories import destinations as destinations_repo
from app.db.repositories import publishing as publishing_repo
from app.db.repositories import queue as queue_repo
from app.db.repositories import scheduler as scheduler_repo
from app.scheduler.scheduler import PublishingScheduler
from app.services import billing
from app.services.pacing import (
    PacingPermit,
    PostgresPacer,
    SendAuthorization,
)
from app.services.publishing import PublishingService
from app.services.queue_requests import (
    QueueRequestService,
    QueueUnsupportedBackend,
    select_catchup_dates,
)
from app.services.recovery import RecoveryService
from app.services.reports import ReportService
from app.services.sender import AmbiguousSendError
from app.services.worker import (
    HEARTBEAT_ABORT,
    HEARTBEAT_LEASE_LOST,
    HEARTBEAT_OK,
    PublishingWorker,
    TransientDBError,
    WorkerLimits,
    heartbeat_with_retry,
)
from tests.helpers import FakeBot, FakeSender, create_destination, create_source


class FakePacer:
    """No-delay pacer used by worker tests."""

    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    async def acquire(self, *, timeout_seconds: float) -> PacingPermit:
        self.acquired += 1
        return PacingPermit(
            token=uuid.uuid4(), expires_at=datetime.now(UTC) + timedelta(seconds=25)
        )

    async def renew(self, permit: PacingPermit) -> bool:
        return True

    async def authorize(
        self, permit: PacingPermit, *, job_id: str, lease_token: uuid.UUID
    ) -> SendAuthorization:
        return SendAuthorization(
            remaining_seconds=25.0, send_started_at=datetime.now(UTC)
        )

    def local_deadline(self, authorization: SendAuthorization) -> float:
        return float("inf")

    def local_deadline_ok(self, deadline: float) -> bool:
        return True

    async def release(
        self, permit: PacingPermit, *, send_started_at: datetime
    ) -> None:
        self.released += 1


class AmbiguousSender(FakeSender):
    """First copy raises a network-class ambiguity; later copies succeed."""

    def __init__(self) -> None:
        super().__init__()
        self.raised = False

    async def copy_message(self, *, chat_id: int, from_chat_id: int, message_id: int) -> int:
        if not self.raised:
            self.raised = True
            raise AmbiguousSendError("simulated ambiguous send")
        return await super().copy_message(
            chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id
        )


class TransactionProbeSender(FakeSender):
    """Records whether any database transaction was open during the send."""

    def __init__(self, db: object) -> None:
        super().__init__()
        self._db = db
        self.open_transactions: list[bool] = []

    async def copy_message(self, *, chat_id: int, from_chat_id: int, message_id: int) -> int:
        self.open_transactions.append(
            self._db._tx_conn.get() is not None
        )
        return await super().copy_message(
            chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id
        )


class RecoveringSender(FakeSender):
    """Simulates another worker recovering this job's lease during a send."""

    def __init__(self, db: object, job_id: str) -> None:
        super().__init__()
        self._db = db
        self._job_id = job_id
        self.recovered: queue_repo.RecoveryOutcome | None = None

    async def copy_message(self, *, chat_id: int, from_chat_id: int, message_id: int) -> int:
        await self._db.execute(
            "UPDATE publishing_jobs "
            "SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE id = ?",
            (self._job_id,),
        )
        self.recovered = await queue_repo.recover_expired_job(self._db)
        return await super().copy_message(
            chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id
        )


async def _make_manual_job(pg_db, *, count: int = 3, batch_size: int = 10):
    await create_source(pg_db, count=count)
    destination = await create_destination(pg_db, batch_size=batch_size)
    request = await queue_repo.create_manual_request(
        pg_db,
        request_type="manual",
        requested_by=1,
        destination_id=destination.id,
    )
    return destination, request


async def _claim(pg_db, *, lease_seconds: int = 180):
    return await queue_repo.claim_next_job(
        pg_db,
        worker_id="test-worker",
        lease_token=uuid.uuid4(),
        lease_seconds=lease_seconds,
    )


async def _job_row(pg_db, job_id: str) -> dict:
    row = await pg_db.fetchone("SELECT * FROM publishing_jobs WHERE id = ?", (job_id,))
    assert row is not None
    return row


async def _expire(mutate, job_id: str) -> None:
    await mutate(
        "UPDATE publishing_jobs "
        "SET lease_expires_at = clock_timestamp() - interval '1 second' "
        "WHERE id = ?",
        (job_id,),
    )


# ---------------------------------------------------------------------------
# Manual request creation and coalescing
# ---------------------------------------------------------------------------


async def test_manual_request_creates_one_job_per_destination(pg_db):
    await create_source(pg_db, count=2)
    first = await create_destination(pg_db, chat_id=-1001, batch_size=10)
    second = await create_destination(pg_db, chat_id=-1002, batch_size=10)

    creation = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=7, destination_id=None
    )

    assert creation.cancelled is False
    assert len(creation.job_ids) == 2
    request = await queue_repo.get_request(pg_db, creation.request_id)
    assert request is not None
    assert request["planned_jobs"] == 2
    assert request["requested_by"] == 7
    assert await queue_repo.count_active_manual_jobs(pg_db, first.id) == 1
    assert await queue_repo.count_active_manual_jobs(pg_db, second.id) == 1


async def test_second_manual_request_coalesces_onto_active_job(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    first = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    second = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=2, destination_id=destination.id
    )

    assert first.job_ids == second.job_ids
    assert second.coalesced_job_ids == first.job_ids
    assert await queue_repo.count_active_manual_jobs(pg_db, destination.id) == 1

    second_request = await queue_repo.get_request(pg_db, second.request_id)
    assert second_request is not None
    assert second_request["planned_jobs"] == 1


async def test_resume_coalesces_with_manual_cycle(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    manual = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    resume = await queue_repo.create_manual_request(
        pg_db, request_type="resume", requested_by=1, destination_id=destination.id
    )

    assert resume.job_ids == manual.job_ids
    job = await _job_row(pg_db, manual.job_ids[0])
    assert job["trigger"] == "manual"
    assert await queue_repo.count_active_manual_jobs(pg_db, destination.id) == 1


async def test_manual_request_while_scheduled_running_queues_pending_manual_job(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    await queue_repo.create_scheduled_request(
        pg_db,
        request_type="schedule",
        schedule_date="2026-01-01",
        slot="daily",
        destination_id=destination.id,
    )
    claimed = await _claim(pg_db)
    assert claimed is not None
    assert claimed.trigger == "schedule"

    manual = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    assert manual.job_ids
    manual_job = await _job_row(pg_db, manual.job_ids[0])
    assert manual_job["status"] == "pending"
    assert manual_job["trigger"] == "manual"
    assert await queue_repo.count_active_manual_jobs(pg_db, destination.id) == 1
    assert (await _job_row(pg_db, claimed.id))["status"] == "running"


async def test_manual_request_with_only_paused_destination_is_cancelled(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    await destinations_repo.set_paused(pg_db, destination.chat_id, True)

    creation = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )

    assert creation.cancelled is True
    assert creation.status == "cancelled"
    assert creation.job_ids == ()
    request = await queue_repo.get_request(pg_db, creation.request_id)
    assert request is not None
    assert request["cancelled_reason"] == "no_eligible_destinations"


async def test_concurrent_manual_requests_create_one_active_job(pg_db):
    destination = await create_destination(pg_db, batch_size=10)

    first, second = await asyncio.gather(
        queue_repo.create_manual_request(
            pg_db, request_type="manual", requested_by=1, destination_id=destination.id
        ),
        queue_repo.create_manual_request(
            pg_db, request_type="manual", requested_by=2, destination_id=destination.id
        ),
    )

    assert set(first.job_ids) | set(second.job_ids) == set(first.job_ids)
    assert await queue_repo.count_active_manual_jobs(pg_db, destination.id) == 1
    rows = await pg_db.fetchall(
        "SELECT id FROM publishing_jobs WHERE destination_id = ?", (destination.id,)
    )
    assert len(rows) == 1


async def test_concurrent_multi_destination_requests_do_not_deadlock(pg_db):
    destinations = [
        await create_destination(pg_db, chat_id=-2000 - index, batch_size=10)
        for index in range(3)
    ]

    creations = await asyncio.wait_for(
        asyncio.gather(
            *[
                queue_repo.create_manual_request(
                    pg_db, request_type="manual", requested_by=index, destination_id=None
                )
                for index in range(4)
            ]
        ),
        timeout=15,
    )

    assert all(not creation.cancelled for creation in creations)
    for destination in destinations:
        assert await queue_repo.count_active_manual_jobs(pg_db, destination.id) == 1


# ---------------------------------------------------------------------------
# Scheduled and catch-up dedupe
# ---------------------------------------------------------------------------


async def test_scheduled_request_is_idempotent_for_same_date_and_slot(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    first = await queue_repo.create_scheduled_request(
        pg_db,
        request_type="schedule",
        schedule_date="2026-02-01",
        slot="daily",
        destination_id=destination.id,
    )
    second = await queue_repo.create_scheduled_request(
        pg_db,
        request_type="schedule",
        schedule_date="2026-02-01",
        slot="daily",
        destination_id=destination.id,
    )

    assert first.skipped is False
    assert second.skipped is True
    assert second.request_id == first.request_id
    assert second.job_ids == first.job_ids
    rows = await pg_db.fetchall(
        "SELECT id FROM publishing_jobs WHERE destination_id = ?", (destination.id,)
    )
    assert len(rows) == 1


async def test_scheduled_request_for_another_date_creates_new_job(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    first = await queue_repo.create_scheduled_request(
        pg_db,
        request_type="schedule",
        schedule_date="2026-02-01",
        slot="daily",
        destination_id=destination.id,
    )
    second = await queue_repo.create_scheduled_request(
        pg_db,
        request_type="catchup",
        schedule_date="2026-02-02",
        slot="daily",
        destination_id=destination.id,
    )

    assert first.request_id != second.request_id
    assert set(first.job_ids).isdisjoint(second.job_ids)
    rows = await pg_db.fetchall(
        "SELECT dedupe_key FROM publishing_jobs WHERE destination_id = ?",
        (destination.id,),
    )
    assert {row["dedupe_key"] for row in rows} == {
        f"cycle:2026-02-01:daily:{destination.id}",
        f"cycle:2026-02-02:daily:{destination.id}",
    }


async def test_catchup_selection_is_ordered_and_capped():
    target = date(2026, 2, 10)
    assert select_catchup_dates(None, target) == [target]
    assert select_catchup_dates(date(2026, 2, 1), target) == [
        date(2026, 2, 2),
        date(2026, 2, 3),
        date(2026, 2, 4),
        date(2026, 2, 5),
        date(2026, 2, 6),
        date(2026, 2, 7),
        date(2026, 2, 8),
    ]
    assert select_catchup_dates(date(2026, 2, 9), target) == [date(2026, 2, 10)]


# ---------------------------------------------------------------------------
# Claim, lease and heartbeat
# ---------------------------------------------------------------------------


async def test_claim_follows_priority_then_manual_before_scheduled(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    await queue_repo.create_scheduled_request(
        pg_db,
        request_type="schedule",
        schedule_date="2026-03-01",
        slot="daily",
        destination_id=destination.id,
    )
    manual = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )

    claimed = await _claim(pg_db)

    assert claimed is not None
    assert claimed.trigger == "manual"
    assert claimed.id in manual.job_ids


async def test_claim_is_exclusive_per_destination(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    await queue_repo.create_scheduled_request(
        pg_db,
        request_type="schedule",
        schedule_date="2026-03-02",
        slot="daily",
        destination_id=destination.id,
    )
    await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )

    first = await _claim(pg_db)
    second = await _claim(pg_db)

    assert first is not None
    assert second is None
    running = await pg_db.fetchall(
        "SELECT id FROM publishing_jobs WHERE destination_id = ? AND status = 'running'",
        (destination.id,),
    )
    assert len(running) == 1


async def test_claim_respects_available_at(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    claimed = await _claim(pg_db)
    assert claimed is not None
    assert (
        await queue_repo.release_job(
            pg_db,
            job_id=claimed.id,
            lease_token=claimed.lease_token,
            error="later",
            available_after_seconds=3600,
            increment_retry=False,
        )
        == "retry"
    )
    assert await _claim(pg_db) is None

    await pg_db.execute(
        "UPDATE publishing_jobs SET available_at = clock_timestamp() - interval '1 second' "
        "WHERE id = ?",
        (claimed.id,),
    )
    assert await _claim(pg_db) is not None


async def test_heartbeat_extends_lease_and_rejects_wrong_token(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    claimed = await _claim(pg_db)
    assert claimed is not None
    before = (await _job_row(pg_db, claimed.id))["lease_expires_at"]

    assert await queue_repo.heartbeat_job(
        pg_db, job_id=claimed.id, lease_token=claimed.lease_token
    )
    after = (await _job_row(pg_db, claimed.id))["lease_expires_at"]
    assert after >= before

    assert not await queue_repo.heartbeat_job(
        pg_db, job_id=claimed.id, lease_token=uuid.uuid4()
    )


async def test_finish_job_requires_matching_lease_token(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    claimed = await _claim(pg_db)
    assert claimed is not None

    assert not await queue_repo.finish_job(
        pg_db,
        job_id=claimed.id,
        lease_token=uuid.uuid4(),
        status="completed",
    )
    assert (await _job_row(pg_db, claimed.id))["status"] == "running"

    assert await queue_repo.finish_job(
        pg_db,
        job_id=claimed.id,
        lease_token=claimed.lease_token,
        status="completed",
        outcome="done",
    )
    row = await _job_row(pg_db, claimed.id)
    assert row["status"] == "completed"
    assert row["lease_token"] is None
    assert row["finished_at"] is not None


async def test_release_job_retries_then_fails_at_limit(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    creation = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    job_id = creation.job_ids[0]
    await pg_db.execute(
        "UPDATE publishing_jobs SET max_retries = 1 WHERE id = ?", (job_id,)
    )

    first = await _claim(pg_db)
    assert first is not None
    assert (
        await queue_repo.release_job(
            pg_db,
            job_id=first.id,
            lease_token=first.lease_token,
            error="boom",
        )
        == "retry"
    )
    assert (await _job_row(pg_db, job_id))["retry_count"] == 1

    second = await _claim(pg_db)
    assert second is not None
    assert (
        await queue_repo.release_job(
            pg_db,
            job_id=second.id,
            lease_token=second.lease_token,
            error="boom again",
        )
        == "failed"
    )
    row = await _job_row(pg_db, job_id)
    assert row["status"] == "failed"
    assert row["outcome"] == "retry_exhausted"


# ---------------------------------------------------------------------------
# Recovery and uncertainty
# ---------------------------------------------------------------------------


async def test_recover_expired_job_retries_when_no_send_started(pg_db):
    destination, _creation = await _make_manual_job(pg_db)
    claimed = await _claim(pg_db)
    assert claimed is not None
    run_id = uuid.uuid4().hex
    await publishing_repo.create_run(
        pg_db,
        run_id=run_id,
        destination_id=destination.id,
        trigger="manual",
        status=RUN_RUNNING,
        planned_count=1,
    )
    await queue_repo.attach_run(
        pg_db, job_id=claimed.id, lease_token=claimed.lease_token, run_id=run_id
    )
    await _expire(pg_db.execute, claimed.id)

    recovered = await queue_repo.recover_expired_job(pg_db)

    assert recovered is not None
    assert recovered.action == "retry"
    row = await _job_row(pg_db, claimed.id)
    assert row["status"] == "retry"
    assert row["run_id"] is None
    run = await publishing_repo.get_run(pg_db, run_id)
    assert run is not None
    assert run.status == RUN_INCOMPLETE


async def test_recover_expired_job_marks_ambiguous_send_uncertain(pg_db):
    destination, _ = await _make_manual_job(pg_db)
    claimed = await _claim(pg_db)
    assert claimed is not None

    run_id = uuid.uuid4().hex
    await publishing_repo.create_run(
        pg_db,
        run_id=run_id,
        destination_id=destination.id,
        trigger="manual",
        status=RUN_RUNNING,
        planned_count=1,
    )
    candidates = await publishing_repo.list_candidates(
        pg_db, destination_id=destination.id, next_sequence=1, limit=1
    )
    reservation, history_id = await publishing_repo.reserve_for_send(
        pg_db,
        destination_id=destination.id,
        source=candidates[0].source,
        run_id=run_id,
    )
    assert reservation == "booked"
    assert history_id is not None
    assert await queue_repo.tag_history_units(
        pg_db,
        history_ids=[history_id],
        unit_id=uuid.uuid4(),
        lease_token=claimed.lease_token,
    )
    await queue_repo.attach_run(
        pg_db, job_id=claimed.id, lease_token=claimed.lease_token, run_id=run_id
    )
    await _expire(pg_db.execute, claimed.id)

    recovered = await queue_repo.recover_expired_job(pg_db)

    assert recovered is not None
    assert recovered.action == "uncertain"
    assert recovered.uncertain_messages == 1
    history = await pg_db.fetchone(
        "SELECT status, lease_token FROM publishing_history WHERE id = ?", (history_id,)
    )
    assert history is not None
    assert history["status"] == MSG_UNCERTAIN
    assert history["lease_token"] is None
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None
    assert queue.next_sequence == 2
    run = await publishing_repo.get_run(pg_db, run_id)
    assert run is not None
    assert run.status == RUN_COMPLETED_WITH_ERRORS
    assert run.uncertain_count == 1
    job = await _job_row(pg_db, claimed.id)
    assert job["status"] == "completed_with_errors"
    assert job["outcome"] == "uncertain"


async def test_recover_expired_job_fails_after_retry_limit(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    creation = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    await pg_db.execute(
        "UPDATE publishing_jobs SET max_retries = 0 WHERE id = ?",
        (creation.job_ids[0],),
    )
    claimed = await _claim(pg_db)
    assert claimed is not None
    await _expire(pg_db.execute, claimed.id)

    recovered = await queue_repo.recover_expired_job(pg_db)

    assert recovered is not None
    assert recovered.action == "failed"
    row = await _job_row(pg_db, claimed.id)
    assert row["status"] == "failed"
    assert row["outcome"] == "retry_exhausted"


# ---------------------------------------------------------------------------
# Global pacing
# ---------------------------------------------------------------------------


async def test_pacer_allows_one_permit_and_spaces_release(pg_db):
    pacer = PostgresPacer(pg_db, spacing_seconds=0.05, ttl_seconds=25)
    permit = await pacer.acquire(timeout_seconds=1.0)
    assert permit is not None
    assert await pacer.acquire(timeout_seconds=0.0) is None

    await pacer.release(permit, send_started_at=datetime.now(UTC))
    assert await pacer.acquire(timeout_seconds=0.0) is None

    await asyncio.sleep(0.08)
    second = await pacer.acquire(timeout_seconds=1.0)
    assert second is not None
    await pacer.release(second, send_started_at=datetime.now(UTC))


async def test_pacer_renew_rejects_expired_or_replaced_permit(pg_db):
    pacer = PostgresPacer(pg_db, spacing_seconds=0.0, ttl_seconds=25)
    permit = await pacer.acquire(timeout_seconds=1.0)
    assert permit is not None
    assert await pacer.renew(permit)

    await pg_db.execute(
        "UPDATE telegram_rate_limit "
        "SET permit_expires_at = clock_timestamp() - interval '1 second' WHERE id = 1"
    )
    assert not await pacer.renew(permit)

    replacement = await pacer.acquire(timeout_seconds=1.0)
    assert replacement is not None
    assert not await pacer.renew(permit)
    await pacer.release(replacement, send_started_at=datetime.now(UTC))


async def test_pacer_authorize_requires_running_job_and_matching_token(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    claimed = await _claim(pg_db)
    assert claimed is not None
    pacer = PostgresPacer(pg_db, spacing_seconds=0.0)
    permit = await pacer.acquire(timeout_seconds=1.0)
    assert permit is not None

    authorization = await pacer.authorize(
        permit, job_id=claimed.id, lease_token=claimed.lease_token
    )
    assert authorization is not None
    assert authorization.remaining_seconds > 0
    stored = await _job_row(pg_db, claimed.id)
    assert stored["send_started_at"] is not None

    assert (
        await pacer.authorize(
            permit, job_id=uuid.uuid4().hex, lease_token=claimed.lease_token
        )
        is None
    )
    assert (
        await pacer.authorize(
            permit, job_id=claimed.id, lease_token=uuid.uuid4()
        )
        is None
    )
    await pacer.release(permit, send_started_at=authorization.send_started_at)


async def test_pacer_local_deadline_includes_one_second_margin(pg_db):
    clock = [0.0]
    pacer = PostgresPacer(pg_db, monotonic=lambda: clock[0])
    authorization = SendAuthorization(
        remaining_seconds=2.0, send_started_at=datetime.now(UTC)
    )

    deadline = pacer.local_deadline(authorization)
    assert deadline == pytest.approx(1.0)
    assert pacer.local_deadline_ok(deadline)
    clock[0] = 1.0
    assert not pacer.local_deadline_ok(deadline)


# ---------------------------------------------------------------------------
# Billing accounting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (0.0, 0),
        (0.5, 60),
        (1.0, 60),
        (59.9, 60),
        (60.0, 60),
        (60.1, 120),
        (3599.0, 3600),
        (3601.0, 3600),
    ],
)
def test_charged_seconds_rounds_each_run_up_and_caps(actual, expected):
    assert billing.charged_seconds(actual) == expected


def test_split_charge_preserves_total_and_cap():
    assert billing.split_charge(0) == (0, 0)
    assert billing.split_charge(60) == (60, 0)
    assert billing.split_charge(600) == (120, 480)
    assert billing.split_charge(10_000) == (120, 3480)


@pytest.mark.parametrize("days", [30, 31])
def test_budget_model_uses_per_run_rounding(days):
    budget = billing.model_budget(days=days, retry_rate=0.0)
    assert budget.scheduled_minutes == days * 52
    assert budget.abandoned_uplift_minutes == 32
    assert budget.manual_minutes == 120
    assert budget.ledger_minutes == days * 52 + 32 + 120
    assert budget.within_internal_cap is True


def test_budget_model_reduces_manual_before_scheduled_when_tight():
    budget = billing.model_budget(days=31, retry_rate=0.10)
    assert budget.scheduled_minutes == 31 * 52
    assert budget.retry_minutes == 161
    assert budget.abandoned_uplift_minutes == 32
    assert budget.manual_minutes == 95
    assert budget.ledger_minutes == 1900
    assert budget.within_internal_cap is True


def test_budget_model_rejects_unsupported_month_and_negative_retry():
    with pytest.raises(ValueError, match="30-day and 31-day"):
        billing.model_budget(days=28)
    with pytest.raises(ValueError, match="non-negative"):
        billing.model_budget(days=30, retry_rate=-0.1)


async def test_admission_reserves_a_full_platform_timeout(pg_db):
    month = billing.month_key()
    await pg_db.execute(
        "INSERT INTO worker_runs("
        "id, worker_id, status, month_key, started_at, setup_seconds, work_seconds"
        ") VALUES (?, ?, 'completed', ?, clock_timestamp(), 120, ?)",
        ("prior", "worker", month, 110_340 - 120),
    )

    # 110340 committed + 3600 reserved = 113940 <= 114000: admitted.
    first = await billing.admit_worker_run(pg_db, worker_id="a", key=month)
    assert first is not None

    # The running row itself reserves another 3600, so a second overlapping
    # invocation is refused instead of over-admitting past the internal cap.
    assert await billing.admit_worker_run(pg_db, worker_id="b", key=month) is None

    # Finishing charges only the ceiling-rounded 60 seconds; the remaining
    # reservation is returned to the ledger.
    assert (
        await billing.finish_worker_run(
            pg_db, run_id=first, actual_seconds=1.0, status="completed"
        )
        == 60
    )
    # 110340 + 60 + 3600 = 114000 exactly: admitted at the boundary.
    second = await billing.admit_worker_run(pg_db, worker_id="b", key=month)
    assert second is not None
    assert await billing.admit_worker_run(pg_db, worker_id="c", key=month) is None


async def test_admission_refuses_when_committed_ledger_exceeds_cap(pg_db):
    month = billing.month_key()
    await pg_db.execute(
        "INSERT INTO worker_runs("
        "id, worker_id, status, month_key, started_at, setup_seconds, work_seconds"
        ") VALUES (?, ?, 'completed', ?, clock_timestamp(), 120, ?)",
        ("over", "worker", month, 110_401 - 120),
    )
    assert await billing.admit_worker_run(pg_db, worker_id="x", key=month) is None


async def test_admission_reserves_the_full_cap_even_when_caller_asks_for_less(pg_db):
    month = billing.month_key()
    await pg_db.execute(
        "INSERT INTO worker_runs("
        "id, worker_id, status, month_key, started_at, setup_seconds, work_seconds"
        ") VALUES (?, ?, 'completed', ?, clock_timestamp(), 120, ?)",
        ("used", "worker", month, 111_000 - 120),
    )

    # 111000 committed: a 1-second reservation would fit, but the inserted
    # ``running`` row is charged 3600 seconds, so admission must reserve that
    # full amount and refuse instead of over-committing the internal cap.
    assert (
        await billing.admit_worker_run(pg_db, worker_id="x", key=month, reserve_seconds=1)
        is None
    )

    # Boundary check: 110400 committed + the full 3600 reservation fits exactly.
    await pg_db.execute("DELETE FROM worker_runs")
    await pg_db.execute(
        "INSERT INTO worker_runs("
        "id, worker_id, status, month_key, started_at, setup_seconds, work_seconds"
        ") VALUES (?, ?, 'completed', ?, clock_timestamp(), 120, ?)",
        ("boundary", "worker", month, 110_400 - 120),
    )
    assert (
        await billing.admit_worker_run(pg_db, worker_id="x", key=month, reserve_seconds=1)
        is not None
    )


async def test_finish_worker_run_rounds_and_abandoned_caps(pg_db):
    month = billing.month_key()
    run_id = await billing.admit_worker_run(pg_db, worker_id="w", key=month)
    assert run_id is not None
    charged = await billing.finish_worker_run(
        pg_db,
        run_id=run_id,
        actual_seconds=61.2,
        status="completed",
        claimed_jobs=4,
    )
    assert charged == 120
    row = await pg_db.fetchone("SELECT * FROM worker_runs WHERE id = ?", (run_id,))
    assert row is not None
    assert row["setup_seconds"] + row["work_seconds"] == 120
    assert row["claimed_jobs"] == 4

    abandoned_id = await billing.admit_worker_run(pg_db, worker_id="w", key=month)
    assert abandoned_id is not None
    assert (
        await billing.finish_worker_run(
            pg_db, run_id=abandoned_id, actual_seconds=5.0, status="abandoned"
        )
        == 3600
    )


async def test_reaper_charges_stale_running_rows_at_cap(pg_db):
    await pg_db.execute(
        "INSERT INTO worker_runs(id, worker_id, status, month_key, started_at) "
        "VALUES (?, ?, 'running', ?, clock_timestamp() - interval '2 hours')",
        (uuid.uuid4().hex, "dead-worker", billing.month_key()),
    )

    assert await billing.reap_abandoned_runs(pg_db) == 1
    row = await pg_db.fetchone(
        "SELECT * FROM worker_runs WHERE worker_id = ?", ("dead-worker",)
    )
    assert row is not None
    assert row["status"] == "abandoned"
    assert row["setup_seconds"] + row["work_seconds"] == 3600
    assert row["finished_at"] is not None


# ---------------------------------------------------------------------------
# Worker behaviour
# ---------------------------------------------------------------------------


def test_worker_refuses_sqlite_without_explicit_test_override(db, settings: Settings):
    with pytest.raises(QueueUnsupportedBackend, match="requires PostgreSQL"):
        PublishingWorker(db, FakeSender(), settings)
    PublishingWorker(db, FakeSender(), settings, allow_sqlite_test=True)


async def test_worker_sends_batch_and_completes_job(pg_db, settings: Settings):
    destination, creation = await _make_manual_job(pg_db, count=5, batch_size=10)
    sender = FakeSender()

    report = await PublishingWorker(
        pg_db, sender, settings, pacer=FakePacer()
    ).run()

    assert report.admitted is True
    assert report.send_attempts == 5
    assert report.completed_jobs == 1
    assert report.stopped_reason == "idle"
    assert report.charged_seconds == 60
    assert len(sender.sent) == 5

    job = await _job_row(pg_db, creation.job_ids[0])
    assert job["status"] == "completed"
    assert job["lease_token"] is None
    request = await queue_repo.get_request(pg_db, creation.request_id)
    assert request is not None
    assert request["status"] == "completed"
    assert request["completed_jobs"] == 1

    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None
    assert queue.next_sequence == 6
    history = await pg_db.fetchall(
        "SELECT status, unit_id, lease_token FROM publishing_history"
    )
    assert {row["status"] for row in history} == {MSG_SENT}
    assert all(row["unit_id"] is not None for row in history)
    assert all(row["lease_token"] is None for row in history)

    worker_row = await pg_db.fetchone(
        "SELECT * FROM worker_runs WHERE id = ?", (report.worker_run_id,)
    )
    assert worker_row is not None
    assert worker_row["status"] == "completed"
    assert worker_row["setup_seconds"] + worker_row["work_seconds"] == 60


async def test_worker_batch_size_limits_each_cycle(pg_db, settings: Settings):
    await create_source(pg_db, count=8)
    destination = await create_destination(pg_db, batch_size=3)
    sender = FakeSender()

    first = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    await PublishingWorker(pg_db, sender, settings, pacer=FakePacer()).run()
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None and queue.next_sequence == 4
    assert len(sender.sent) == 3
    first_request = await queue_repo.get_request(pg_db, first.request_id)
    assert first_request is not None and first_request["status"] == "completed"

    await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    await PublishingWorker(pg_db, sender, settings, pacer=FakePacer()).run()
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None and queue.next_sequence == 7
    assert len(sender.sent) == 6


async def test_worker_groups_albums_into_one_copy_messages_call(pg_db, settings: Settings):
    await create_source(pg_db, count=4, album_every=4, album_size=3)
    destination = await create_destination(pg_db, batch_size=10)
    await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )
    sender = FakeSender()

    report = await PublishingWorker(pg_db, sender, settings, pacer=FakePacer()).run()

    assert report.send_attempts == 2
    assert len(sender.albums) == 1
    assert sender.albums[0][2] == (1, 2, 3)
    assert len(sender.sent) == 4
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None and queue.next_sequence == 5


async def test_worker_transient_failure_retries_without_advancing_cursor(pg_db, settings: Settings):
    destination, creation = await _make_manual_job(pg_db, count=2, batch_size=10)
    sender = FakeSender()
    sender.transient_failures[1] = 100

    report = await PublishingWorker(pg_db, sender, settings, pacer=FakePacer()).run()

    assert report.retry_jobs == 1
    assert report.send_attempts == 3
    job = await _job_row(pg_db, creation.job_ids[0])
    assert job["status"] == "retry"
    assert job["retry_count"] == 1
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None and queue.next_sequence == 1
    run = await publishing_repo.list_runs(pg_db, destination_id=destination.id, limit=1)
    assert run[0].status == RUN_INCOMPLETE


async def test_worker_permanent_failure_advances_and_completes_with_errors(
    pg_db, settings: Settings
):
    destination, creation = await _make_manual_job(pg_db, count=3, batch_size=10)
    sender = FakeSender()
    sender.permanent_failures[2] = "blocked"

    report = await PublishingWorker(pg_db, sender, settings, pacer=FakePacer()).run()

    assert report.completed_jobs == 1
    assert len(sender.sent) == 2
    job = await _job_row(pg_db, creation.job_ids[0])
    assert job["status"] == "completed_with_errors"
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None and queue.next_sequence == 4
    run = await publishing_repo.list_runs(pg_db, destination_id=destination.id, limit=1)
    assert run[0].status == RUN_COMPLETED_WITH_ERRORS
    assert run[0].failed_count == 1


async def test_worker_ambiguous_send_records_uncertain_and_advances(pg_db, settings: Settings):
    destination, creation = await _make_manual_job(pg_db, count=2, batch_size=10)
    sender = AmbiguousSender()

    report = await PublishingWorker(pg_db, sender, settings, pacer=FakePacer()).run()

    assert report.uncertain_messages == 1
    job = await _job_row(pg_db, creation.job_ids[0])
    assert job["status"] == "completed_with_errors"
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None and queue.next_sequence == 2
    history = await pg_db.fetchone(
        "SELECT status FROM publishing_history ORDER BY id LIMIT 1"
    )
    assert history is not None and history["status"] == MSG_UNCERTAIN
    # The worker stops the cycle at the first ambiguous send; nothing after it
    # is attempted until the next cycle.
    assert sender.sent == []


async def test_worker_attempt_cap_stops_run_and_releases_job(pg_db, settings: Settings):
    destination, creation = await _make_manual_job(pg_db, count=10, batch_size=10)
    sender = FakeSender()
    limits = WorkerLimits(max_send_attempts=3)

    report = await PublishingWorker(
        pg_db, sender, settings, pacer=FakePacer(), limits=limits
    ).run()

    assert report.send_attempts == 3
    assert report.stopped_reason == "attempt_limit"
    assert len(sender.sent) == 3
    job = await _job_row(pg_db, creation.job_ids[0])
    assert job["status"] == "retry"
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None and queue.next_sequence == 4


async def test_worker_work_soft_deadline_prevents_sends(pg_db, settings: Settings):
    destination, creation = await _make_manual_job(pg_db, count=3, batch_size=10)
    sender = FakeSender()

    report = await PublishingWorker(
        pg_db,
        sender,
        settings,
        pacer=FakePacer(),
        limits=WorkerLimits(work_soft_deadline_seconds=0),
    ).run()

    assert report.stopped_reason == "work_soft_deadline"
    assert report.send_attempts == 0
    assert sender.sent == []
    job = await _job_row(pg_db, creation.job_ids[0])
    assert job["status"] == "retry"
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None and queue.next_sequence == 1


async def test_worker_wall_deadline_stops_before_claiming(pg_db, settings: Settings):
    await create_destination(pg_db, batch_size=10)
    report = await PublishingWorker(
        pg_db,
        FakeSender(),
        settings,
        pacer=FakePacer(),
        limits=WorkerLimits(wall_soft_deadline_seconds=0),
    ).run()

    assert report.stopped_reason == "wall_soft_deadline"
    assert report.claimed_jobs == 0


async def test_worker_sends_without_an_open_database_transaction(pg_db, settings: Settings):
    await _make_manual_job(pg_db, count=2, batch_size=10)
    sender = TransactionProbeSender(pg_db)

    report = await PublishingWorker(pg_db, sender, settings, pacer=FakePacer()).run()

    assert report.completed_jobs == 1
    assert sender.open_transactions == [False, False]


async def test_worker_does_not_leave_an_orphan_run_when_the_lease_is_lost_at_start(
    pg_db, settings: Settings, monkeypatch
):
    await create_source(pg_db, count=2)
    destination = await create_destination(pg_db, batch_size=10)
    await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )

    async def lost_attach(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(queue_repo, "attach_run", lost_attach)

    report = await PublishingWorker(
        pg_db, FakeSender(), settings, pacer=FakePacer()
    ).run()

    assert report.claimed_jobs == 1
    assert report.stopped_reason == "lease_lost"
    # Run creation is part of the same transaction as the job attachment, so a
    # failed attachment must not persist a ``running`` run that no job (and
    # therefore no lease recovery) can ever finish.
    runs = await pg_db.fetchall("SELECT id FROM publishing_runs")
    assert runs == []


async def test_worker_cannot_overwrite_a_run_recovered_by_another_worker(
    pg_db, settings: Settings
):
    destination, creation = await _make_manual_job(pg_db, count=1, batch_size=10)
    job_id = creation.job_ids[0]
    sender = RecoveringSender(pg_db, job_id)

    report = await PublishingWorker(pg_db, sender, settings, pacer=FakePacer()).run()

    assert report.stopped_reason == "lease_lost"
    assert sender.recovered is not None
    assert sender.recovered.action == "uncertain"

    # The recovering worker already finalised the job and run. The stale worker
    # must not write its own outcome over that state.
    job = await _job_row(pg_db, job_id)
    assert job["status"] == "completed_with_errors"
    history = await pg_db.fetchone("SELECT status FROM publishing_history")
    assert history is not None and history["status"] == MSG_UNCERTAIN
    runs = await publishing_repo.list_runs(pg_db, destination_id=destination.id, limit=10)
    assert len(runs) == 1
    assert runs[0].status == RUN_COMPLETED_WITH_ERRORS
    assert runs[0].uncertain_count == 1
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None and queue.next_sequence == 2


async def test_startup_recovery_is_skipped_on_postgres(pg_db):
    await create_source(pg_db, count=2)
    destination = await create_destination(pg_db, batch_size=10)
    run_id = "active-worker-run"
    await publishing_repo.create_run(
        pg_db,
        run_id=run_id,
        destination_id=destination.id,
        trigger="manual",
        status=RUN_RUNNING,
        planned_count=2,
    )
    candidates = await publishing_repo.list_candidates(
        pg_db, destination_id=destination.id, next_sequence=1, limit=10
    )
    reservation, history_id = await publishing_repo.reserve_for_send(
        pg_db,
        destination_id=destination.id,
        source=candidates[0].source,
        run_id=run_id,
    )
    assert reservation == "booked"
    assert history_id is not None
    await scheduler_repo.claim_scheduled_run(
        pg_db,
        schedule_date="2026-05-01",
        destination_id=destination.id,
        trigger="schedule",
    )

    report = await RecoveryService(pg_db).recover()

    # The legacy startup sweep belongs to the SQLite inline engine. On
    # PostgreSQL it would mark an *active* worker's sending row uncertain.
    assert (
        report.uncertain_messages,
        report.aborted_runs,
        report.affected_destinations,
        report.reset_scheduled_runs,
    ) == (0, 0, 0, 0)
    history = await pg_db.fetchone(
        "SELECT status FROM publishing_history WHERE id = ?", (history_id,)
    )
    assert history is not None and history["status"] == MSG_SENDING
    queue = await destinations_repo.get_queue(pg_db, destination.id)
    assert queue is not None and queue.next_sequence == 1
    run = await publishing_repo.get_run(pg_db, run_id)
    assert run is not None and run.status == RUN_RUNNING
    assert (
        await scheduler_repo.get_scheduled_run(pg_db, "2026-05-01", destination.id)
        == SCHED_RUNNING
    )


async def test_worker_empty_queue_completes_without_messages(pg_db, settings: Settings):
    destination = await create_destination(pg_db, batch_size=10)
    creation = await queue_repo.create_manual_request(
        pg_db, request_type="manual", requested_by=1, destination_id=destination.id
    )

    report = await PublishingWorker(
        pg_db, FakeSender(), settings, pacer=FakePacer()
    ).run()

    assert report.completed_jobs == 1
    job = await _job_row(pg_db, creation.job_ids[0])
    assert job["status"] == "completed"
    assert job["outcome"] == "no_messages"
    run = await publishing_repo.list_runs(pg_db, destination_id=destination.id, limit=1)
    assert run[0].status == RUN_NO_MESSAGES


# ---------------------------------------------------------------------------
# Heartbeat policy and request facade
# ---------------------------------------------------------------------------


async def test_heartbeat_returns_immediately_on_success():
    calls = 0
    sleeps: list[float] = []

    async def beat() -> bool:
        nonlocal calls
        calls += 1
        return True

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    assert await heartbeat_with_retry(beat, sleep=sleep) == HEARTBEAT_OK
    assert calls == 1
    assert sleeps == []


async def test_heartbeat_does_not_retry_a_zero_row_result():
    calls = 0

    async def beat() -> bool:
        nonlocal calls
        calls += 1
        return False

    assert await heartbeat_with_retry(beat) == HEARTBEAT_LEASE_LOST
    assert calls == 1


async def test_heartbeat_retries_transient_failures_with_1s_and_2s():
    calls = 0
    sleeps: list[float] = []

    async def beat() -> bool:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientDBError("pool blip")
        return True

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    assert await heartbeat_with_retry(beat, sleep=sleep) == HEARTBEAT_OK
    assert calls == 3
    assert sleeps == [1.0, 2.0]


async def test_heartbeat_aborts_after_three_transient_failures():
    calls = 0
    sleeps: list[float] = []

    async def beat() -> bool:
        nonlocal calls
        calls += 1
        raise TransientDBError("pool blip")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    assert await heartbeat_with_retry(beat, sleep=sleep) == HEARTBEAT_ABORT
    assert calls == 3
    assert sleeps == [1.0, 2.0]


async def test_queue_request_service_refuses_sqlite(db):
    service = QueueRequestService(db)
    with pytest.raises(QueueUnsupportedBackend, match="requires the PostgreSQL"):
        await service.create_manual_request(request_type="manual", requested_by=1)


async def test_queue_request_service_creates_manual_request(pg_db):
    destination = await create_destination(pg_db, batch_size=10)
    service = QueueRequestService(pg_db)

    creation = await service.create_manual_request(
        request_type="manual", requested_by=1, destination_id=destination.id
    )

    assert creation.job_ids
    assert creation.cancelled is False
    assert await queue_repo.count_active_manual_jobs(pg_db, destination.id) == 1


async def test_pg_scheduler_queues_the_cycle_instead_of_publishing(pg_db, settings: Settings):
    await create_destination(pg_db, batch_size=10)
    reports = ReportService(pg_db, FakeBot(), settings)
    scheduler = PublishingScheduler(
        pg_db, settings, PublishingService(pg_db, FakeSender(), settings), reports
    )

    results = await scheduler.run_cycle("2026-04-01", "schedule")

    assert results == []
    request = await pg_db.fetchone(
        "SELECT id, status FROM publish_requests "
        "WHERE request_type = 'schedule' AND schedule_date = '2026-04-01'"
    )
    assert request is not None
    job = await pg_db.fetchone(
        "SELECT status FROM publishing_jobs WHERE schedule_date = '2026-04-01'"
    )
    assert job is not None
    assert job["status"] == "pending"
    assert await scheduler_repo.get_last_cycle_date(pg_db) == "2026-04-01"

    # A duplicate scheduler trigger is idempotent and does not add a second job.
    assert await scheduler.run_cycle("2026-04-01", "schedule") == []
    jobs = await pg_db.fetchall(
        "SELECT id FROM publishing_jobs WHERE schedule_date = '2026-04-01'"
    )
    assert len(jobs) == 1


def test_worker_limits_are_the_approved_phase2_defaults():
    limits = WorkerLimits()
    assert limits.max_send_attempts == 950
    assert limits.work_soft_deadline_seconds == 3000
    assert limits.wall_soft_deadline_seconds == 3120
    assert limits.hard_deadline_seconds == 3480
    assert limits.lease_seconds == 180
    assert limits.heartbeat_interval_seconds == 30


def test_sender_ambiguity_is_a_transient_subclass():
    from app.services.sender import TransientSendError

    assert issubclass(AmbiguousSendError, TransientSendError)


def test_source_message_model_exposes_sequence_identity():
    source = SourceMessage(
        id=7,
        source_chat_id=1,
        chat_id=-100,
        message_id=7,
        media_group_id=None,
        content_type="text",
        has_media=False,
        caption=None,
        text_content="x",
        message_date=None,
        indexed_at="2026-01-01T00:00:00+00:00",
    )
    assert source.id == 7

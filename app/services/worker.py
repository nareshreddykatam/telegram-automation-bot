"""Bounded PostgreSQL publishing worker.

The worker runs one short, budgeted pass per GitHub Actions invocation. It
claims jobs atomically, sends through the global pacer, heartbeats the 180-second
lease, and exits at the configured attempt/deadline limits. It never holds a
database transaction across a Telegram call or a sleep.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.constants import (
    MAX_MEDIA_GROUP_SIZE,
    MSG_RETRY,
    MSG_UNCERTAIN,
    RUN_COMPLETED,
    RUN_COMPLETED_WITH_ERRORS,
    RUN_FAILED,
    RUN_INCOMPLETE,
    RUN_NO_MESSAGES,
    RUN_RUNNING,
)
from app.db.database import Database
from app.db.models import Destination, FailedMessage
from app.db.repositories import destinations as destinations_repo
from app.db.repositories import publishing as publishing_repo
from app.db.repositories import queue as queue_repo
from app.db.repositories import scheduler as scheduler_repo
from app.db.repositories.queue import ClaimedJob, RecoveryOutcome, WorkerReport
from app.services import billing
from app.services.pacing import (
    SEND_SPACING_SECONDS,
    PostgresPacer,
    SendAuthorization,
)
from app.services.publishing import PublishingService, WorkUnit
from app.services.queue_requests import QueueUnsupportedBackend
from app.services.sender import (
    AmbiguousSendError,
    FloodWaitError,
    PermanentSendError,
    TransientSendError,
)

logger = logging.getLogger(__name__)

MAX_SEND_ATTEMPTS = 950
WORK_SOFT_DEADLINE_SECONDS = 50 * 60
WALL_SOFT_DEADLINE_SECONDS = 52 * 60
HARD_DEADLINE_SECONDS = 58 * 60
LEASE_SECONDS = 180
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_ATTEMPTS = 3
HEARTBEAT_DELAYS = (1.0, 2.0)
TELEGRAM_SEND_TIMEOUT_SECONDS = 20.0
RETRY_BACKOFF_SECONDS = 60.0
CONTROL_POLL_SECONDS = 0.05

HEARTBEAT_OK = "ok"
HEARTBEAT_LEASE_LOST = "lease_lost"
HEARTBEAT_ABORT = "abort"


class TransientDBError(RuntimeError):
    """A database failure that may succeed on a bounded retry."""


class _AttemptLimitReached(Exception):
    pass


class _DeadlineReached(Exception):
    pass


class _PermitLost(Exception):
    pass


@dataclass(slots=True)
class UnitOutcome:
    result: str
    success: int = 0
    failed: int = 0
    uncertain: int = 0


@dataclass(frozen=True, slots=True)
class WorkerLimits:
    max_send_attempts: int = MAX_SEND_ATTEMPTS
    work_soft_deadline_seconds: int = WORK_SOFT_DEADLINE_SECONDS
    wall_soft_deadline_seconds: int = WALL_SOFT_DEADLINE_SECONDS
    hard_deadline_seconds: int = HARD_DEADLINE_SECONDS
    lease_seconds: int = LEASE_SECONDS
    heartbeat_interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS
    send_timeout_seconds: float = TELEGRAM_SEND_TIMEOUT_SECONDS

    def can_attempt(self, attempts: int) -> bool:
        return attempts < self.max_send_attempts

    @staticmethod
    def projected_work_seconds(attempts: int) -> float:
        """Pacing time plus 90 seconds of non-pacing overhead."""

        return attempts * SEND_SPACING_SECONDS + 90.0

    @classmethod
    def projected_wall_seconds(cls, attempts: int) -> float:
        return cls.projected_work_seconds(attempts) + 90.0


def is_transient_db_error(exc: BaseException) -> bool:
    """Classify driver/pool failures worth 1s/2s heartbeat retries."""

    if isinstance(exc, (TransientDBError, asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    name = type(exc).__name__
    return name in {
        "PostgresConnectionError",
        "InterfaceError",
        "CannotConnectNowError",
        "TooManyConnectionsError",
        "AdminShutdownError",
        "QueryCanceledError",
    }


async def heartbeat_with_retry(
    send_heartbeat: Callable[[], Awaitable[bool]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    transient_error: tuple[type[BaseException], ...] = (TransientDBError,),
    attempts: int = HEARTBEAT_ATTEMPTS,
    delays: tuple[float, ...] = HEARTBEAT_DELAYS,
) -> str:
    """Return immediately on success; retry only transient failures.

    A successful first call performs exactly one heartbeat call. A zero-row
    result means the lease was lost and is never retried.
    """

    for attempt in range(1, attempts + 1):
        try:
            if await send_heartbeat():
                return HEARTBEAT_OK
            return HEARTBEAT_LEASE_LOST
        except transient_error:
            if attempt == attempts:
                return HEARTBEAT_ABORT
            await sleep(delays[attempt - 1])
    return HEARTBEAT_ABORT


class PublishingWorker:
    """One bounded worker pass over the queue."""

    def __init__(
        self,
        db: Database,
        sender: object,
        settings: Settings,
        *,
        pacer: PostgresPacer | None = None,
        limits: WorkerLimits | None = None,
        worker_id: str | None = None,
        allow_sqlite_test: bool = False,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if db.backend != "postgres" and not allow_sqlite_test:
            raise QueueUnsupportedBackend(
                "The Phase 2 worker requires PostgreSQL; SQLite keeps the legacy "
                "inline publishing engine. Pass allow_sqlite_test=True only in a "
                "dedicated backend-guard test."
            )
        self._db = db
        self._sender = sender
        self._settings = settings
        self._limits = limits or WorkerLimits()
        self._pacer = pacer or PostgresPacer(
            db,
            spacing_seconds=SEND_SPACING_SECONDS,
            sleep=sleep,
            monotonic=monotonic,
        )
        self._worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic
        self._run_started = 0.0
        self._work_started: float | None = None

    def _supports_albums(self) -> bool:
        return callable(getattr(self._sender, "copy_messages", None))

    async def run(self) -> WorkerReport:
        worker_run_id = await billing.admit_worker_run(
            self._db, worker_id=self._worker_id
        )
        if worker_run_id is None:
            return WorkerReport(admitted=False, stopped_reason="budget_cap")

        report = WorkerReport(admitted=True, worker_run_id=worker_run_id)
        self._run_started = self._monotonic()
        self._work_started = None
        status = "completed"
        error: BaseException | None = None

        try:
            while True:
                now = self._monotonic()
                if now - self._run_started >= self._limits.hard_deadline_seconds:
                    report.stopped_reason = "hard_deadline"
                    break
                if now - self._run_started >= self._limits.wall_soft_deadline_seconds:
                    report.stopped_reason = "wall_soft_deadline"
                    break
                if not self._limits.can_attempt(report.send_attempts):
                    report.stopped_reason = "attempt_limit"
                    break

                job = await self._claim()
                if job is None:
                    report.stopped_reason = "idle"
                    break
                report.claimed_jobs += 1
                if self._work_started is None:
                    self._work_started = self._monotonic()

                outcome = await self._execute_job(job, report)
                if outcome in {
                    "lease_lost",
                    "paused",
                    "hard_deadline",
                    "wall_soft_deadline",
                    "work_soft_deadline",
                    "attempt_limit",
                }:
                    report.stopped_reason = outcome
                    break
                if outcome == "retry":
                    report.retry_jobs += 1
                    continue
                if outcome in {"completed", "completed_with_errors"}:
                    report.completed_jobs += 1
        except BaseException as exc:
            status = "failed"
            report.errors.append(str(exc))
            report.stopped_reason = "error"
            error = exc
        finally:
            elapsed = max(0.0, self._monotonic() - self._run_started)
            charged = await billing.finish_worker_run(
                self._db,
                run_id=worker_run_id,
                actual_seconds=elapsed,
                status=status,
                claimed_jobs=report.claimed_jobs,
                error=report.errors[-1] if report.errors else None,
            )
            report.charged_seconds = charged

        if error is not None:
            raise error
        return report

    async def _claim(self) -> ClaimedJob | None:
        for attempt in range(3):
            lease_token = uuid.uuid4()
            try:
                return await queue_repo.claim_next_job(
                    self._db,
                    worker_id=self._worker_id,
                    lease_token=lease_token,
                    lease_seconds=self._limits.lease_seconds,
                )
            except Exception as exc:
                if not _is_unique_violation(exc) or attempt == 2:
                    raise
                await self._sleep(0.05 * (attempt + 1))
        return None

    async def _execute_job(
        self, job: ClaimedJob, report: WorkerReport
    ) -> str:
        reason = await self._pause_reason(job.destination_id)
        if reason:
            await queue_repo.release_job(
                self._db,
                job_id=job.id,
                lease_token=job.lease_token,
                error=reason,
                increment_retry=False,
            )
            return "paused"

        destination = await destinations_repo.get_by_id(self._db, job.destination_id)
        if destination is None or not destination.enabled:
            await queue_repo.finish_job(
                self._db,
                job_id=job.id,
                lease_token=job.lease_token,
                status="failed",
                outcome="destination_unavailable",
                error="Destination is missing or disabled",
            )
            return "completed"
        if destination.paused:
            await queue_repo.release_job(
                self._db,
                job_id=job.id,
                lease_token=job.lease_token,
                error="Destination is paused",
                increment_retry=False,
            )
            return "paused"

        run_id = uuid.uuid4().hex
        queue = await destinations_repo.get_queue(self._db, destination.id)
        if queue is None:
            await destinations_repo.ensure_queue(self._db, destination.id)
            queue = await destinations_repo.get_queue(self._db, destination.id)
        next_sequence = queue.next_sequence if queue else 1
        pending = await publishing_repo.pending_count(
            self._db, destination_id=destination.id, next_sequence=next_sequence
        )
        planned = min(destination.batch_size, pending)
        # Attach the job to its run and insert the run in one short transaction
        # so a crash cannot leave a ``running`` run that no job (and therefore
        # no lease recovery) can ever finish.
        async with self._db.transaction():
            attached = await queue_repo.attach_run(
                self._db,
                job_id=job.id,
                lease_token=job.lease_token,
                run_id=run_id,
            )
            if attached:
                await publishing_repo.create_run(
                    self._db,
                    run_id=run_id,
                    destination_id=destination.id,
                    trigger=job.trigger,
                    status=RUN_RUNNING,
                    planned_count=planned,
                )
        if not attached:
            return "lease_lost"

        stop_heartbeat = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job, stop_heartbeat, lease_lost),
            name=f"heartbeat-{job.id}",
        )

        success_count = 0
        failed_count = 0
        uncertain_count = 0
        skipped_count = 0
        failures: list[FailedMessage] = []
        run_error: str | None = None
        outcome = "completed"

        try:
            candidates = await publishing_repo.list_candidates(
                self._db,
                destination_id=destination.id,
                next_sequence=next_sequence,
                limit=destination.batch_size + MAX_MEDIA_GROUP_SIZE,
            )
            if not candidates:
                outcome = "no_messages"
                await self._finish_run(
                    run_id,
                    RUN_NO_MESSAGES,
                    success_count,
                    failed_count,
                    uncertain_count,
                    skipped_count,
                    None,
                    job=job,
                )
                await queue_repo.finish_job(
                    self._db,
                    job_id=job.id,
                    lease_token=job.lease_token,
                    status="completed",
                    outcome="no_messages",
                )
                return "completed"

            processed = 0
            for unit in PublishingService._build_units(candidates):
                if processed >= destination.batch_size:
                    break
                size = len(unit.candidates)
                if processed + size > destination.batch_size and processed > 0:
                    break
                if lease_lost.is_set():
                    outcome = "lease_lost"
                    run_error = "Worker lease was lost"
                    break
                if not self._limits.can_attempt(report.send_attempts):
                    outcome = "attempt_limit"
                    run_error = "Send attempt limit reached"
                    break
                now = self._monotonic()
                if now - self._run_started >= self._limits.hard_deadline_seconds:
                    outcome = "hard_deadline"
                    run_error = "Worker hard deadline reached"
                    break
                if now - self._run_started >= self._limits.wall_soft_deadline_seconds:
                    outcome = "wall_soft_deadline"
                    run_error = "Worker wall soft deadline reached"
                    break
                if (
                    self._work_started is not None
                    and now - self._work_started
                    >= self._limits.work_soft_deadline_seconds
                ):
                    outcome = "work_soft_deadline"
                    run_error = "Worker work soft deadline reached"
                    break
                reason = await self._pause_reason(destination.id)
                if reason:
                    outcome = "paused"
                    run_error = reason
                    break

                unit_result = await self._process_unit(
                    unit,
                    job=job,
                    destination=destination,
                    run_id=run_id,
                    report=report,
                    failures=failures,
                )
                success_count += unit_result.success
                failed_count += unit_result.failed
                uncertain_count += unit_result.uncertain
                processed += size
                if unit_result.result == "lease_lost":
                    outcome = "lease_lost"
                    run_error = "Worker lease was lost during a send"
                    break
                if unit_result.result == "retry":
                    outcome = "retry"
                    run_error = "Transient send failure"
                    break
                if unit_result.result == "uncertain":
                    outcome = "uncertain"
                    run_error = "Ambiguous send recorded as uncertain"
                    break

            run_status = _run_status(
                outcome=outcome,
                success_count=success_count,
                failed_count=failed_count,
                uncertain_count=uncertain_count,
            )
            await self._finish_run(
                run_id,
                run_status,
                success_count,
                failed_count,
                uncertain_count,
                skipped_count,
                run_error,
                job=job,
            )

            if outcome == "lease_lost":
                return "lease_lost"
            if outcome in {"retry", "paused"}:
                await queue_repo.release_job(
                    self._db,
                    job_id=job.id,
                    lease_token=job.lease_token,
                    error=run_error,
                    available_after_seconds=RETRY_BACKOFF_SECONDS,
                    increment_retry=outcome == "retry",
                )
                return outcome
            if outcome in {
                "hard_deadline",
                "wall_soft_deadline",
                "work_soft_deadline",
                "attempt_limit",
            }:
                await queue_repo.release_job(
                    self._db,
                    job_id=job.id,
                    lease_token=job.lease_token,
                    error=run_error,
                    available_after_seconds=RETRY_BACKOFF_SECONDS,
                    increment_retry=False,
                )
                return outcome

            job_status = (
                "completed_with_errors"
                if failed_count or uncertain_count
                else "completed"
            )
            finished = await queue_repo.finish_job(
                self._db,
                job_id=job.id,
                lease_token=job.lease_token,
                status=job_status,
                outcome=outcome,
                error=run_error,
            )
            if not finished:
                return "lease_lost"
            report.uncertain_messages += uncertain_count
            return job_status
        except _AttemptLimitReached:
            await self._finish_run(
                run_id,
                RUN_INCOMPLETE,
                success_count,
                failed_count,
                uncertain_count,
                skipped_count,
                "Send attempt limit reached",
                job=job,
            )
            await queue_repo.release_job(
                self._db,
                job_id=job.id,
                lease_token=job.lease_token,
                error="Send attempt limit reached",
                available_after_seconds=RETRY_BACKOFF_SECONDS,
                increment_retry=False,
            )
            return "attempt_limit"
        except _DeadlineReached as exc:
            await self._finish_run(
                run_id,
                RUN_INCOMPLETE,
                success_count,
                failed_count,
                uncertain_count,
                skipped_count,
                str(exc),
                job=job,
            )
            await queue_repo.release_job(
                self._db,
                job_id=job.id,
                lease_token=job.lease_token,
                error=str(exc),
                available_after_seconds=RETRY_BACKOFF_SECONDS,
                increment_retry=False,
            )
            return "wall_soft_deadline"
        except Exception as exc:
            logger.exception("Worker job %s failed", job.id)
            await self._finish_run(
                run_id,
                RUN_FAILED,
                success_count,
                failed_count,
                uncertain_count,
                skipped_count,
                f"Unexpected worker failure: {exc}",
                job=job,
            )
            with contextlib.suppress(Exception):
                await queue_repo.release_job(
                    self._db,
                    job_id=job.id,
                    lease_token=job.lease_token,
                    error=f"Unexpected worker failure: {exc}",
                    available_after_seconds=RETRY_BACKOFF_SECONDS,
                    increment_retry=True,
                )
            raise
        finally:
            stop_heartbeat.set()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _process_unit(
        self,
        unit: WorkUnit,
        *,
        job: ClaimedJob,
        destination: Destination,
        run_id: str,
        report: WorkerReport,
        failures: list[FailedMessage],
    ) -> UnitOutcome:
        sources = [item.source for item in unit.candidates]
        if (
            unit.is_album
            and self._supports_albums()
            and all(item.history_status in (None, MSG_RETRY) for item in unit.candidates)
        ):
            return await self._process_album(
                sources, job=job, destination=destination, run_id=run_id, report=report
            )
        return await self._process_individual(
            sources,
            job=job,
            destination=destination,
            run_id=run_id,
            report=report,
            failures=failures,
        )

    async def _process_album(
        self,
        sources: list[Any],
        *,
        job: ClaimedJob,
        destination: Destination,
        run_id: str,
        report: WorkerReport,
    ) -> UnitOutcome:
        if len({source.chat_id for source in sources}) != 1:
            return await self._process_individual(
                sources,
                job=job,
                destination=destination,
                run_id=run_id,
                report=report,
                failures=[],
            )
        try:
            _, history_ids = await publishing_repo.reserve_album_for_send(
                self._db,
                destination_id=destination.id,
                sources=sources,
                run_id=run_id,
            )
        except publishing_repo.AlbumBlocked:
            return await self._process_individual(
                sources,
                job=job,
                destination=destination,
                run_id=run_id,
                report=report,
                failures=[],
            )
        unit_id = uuid.uuid4()
        if not await queue_repo.tag_history_units(
            self._db,
            history_ids=history_ids,
            unit_id=unit_id,
            lease_token=job.lease_token,
        ):
            return UnitOutcome("lease_lost")

        source_sequences = [source.id for source in sources]
        try:
            sent_ids = await self._send_with_pacing(
                lambda: self._sender.copy_messages(  # type: ignore[attr-defined]
                    chat_id=destination.chat_id,
                    from_chat_id=sources[0].chat_id,
                    message_ids=[source.message_id for source in sources],
                ),
                job=job,
                report=report,
            )
        except AmbiguousSendError as exc:
            await self._mark_units(
                history_ids,
                job,
                status=MSG_UNCERTAIN,
                destination_id=destination.id,
                source_sequences=source_sequences,
                error=str(exc),
            )
            return UnitOutcome("uncertain", uncertain=len(sources))
        except PermanentSendError as exc:
            ok = await self._mark_units(
                history_ids,
                job,
                status="failed",
                destination_id=destination.id,
                source_sequences=source_sequences,
                error=str(exc),
            )
            if not ok:
                return UnitOutcome("lease_lost")
            return UnitOutcome("failed", failed=len(sources))
        except TransientSendError as exc:
            await self._mark_units(
                history_ids,
                job,
                status=MSG_RETRY,
                destination_id=destination.id,
                source_sequences=[],
                error=str(exc),
                advance_cursor=False,
            )
            return UnitOutcome("retry")

        sent_list = list(sent_ids or [])
        if len(sent_list) < len(sources):
            sent_list.extend([None] * (len(sources) - len(sent_list)))
        ok = await self._mark_units(
            history_ids,
            job,
            status="sent",
            destination_id=destination.id,
            source_sequences=source_sequences,
            sent_message_ids=sent_list[: len(sources)],
            success_sequence=max(source_sequences),
        )
        if not ok:
            return UnitOutcome("lease_lost")
        return UnitOutcome("sent", success=len(sources))

    async def _process_individual(
        self,
        sources: list[Any],
        *,
        job: ClaimedJob,
        destination: Destination,
        run_id: str,
        report: WorkerReport,
        failures: list[FailedMessage],
    ) -> UnitOutcome:
        success = 0
        failed = 0
        uncertain = 0
        for source in sources:
            reservation, history_id = await publishing_repo.reserve_for_send(
                self._db,
                destination_id=destination.id,
                source=source,
                run_id=run_id,
            )
            if reservation == "duplicate":
                await publishing_repo.advance_past(
                    self._db,
                    destination_id=destination.id,
                    source_sequence=source.id,
                )
                continue
            if reservation == "uncertain" or history_id is None:
                await publishing_repo.advance_past(
                    self._db,
                    destination_id=destination.id,
                    source_sequence=source.id,
                )
                uncertain += 1
                continue
            unit_id = uuid.uuid4()
            if not await queue_repo.tag_history_units(
                self._db,
                history_ids=[history_id],
                unit_id=unit_id,
                lease_token=job.lease_token,
            ):
                return UnitOutcome("lease_lost", success, failed, uncertain)
            try:
                sent_message_id = await self._send_with_pacing(
                    lambda source=source: self._sender.copy_message(  # type: ignore[attr-defined]
                        chat_id=destination.chat_id,
                        from_chat_id=source.chat_id,
                        message_id=source.message_id,
                    ),
                    job=job,
                    report=report,
                )
            except AmbiguousSendError as exc:
                await self._mark_units(
                    [history_id],
                    job,
                    status=MSG_UNCERTAIN,
                    destination_id=destination.id,
                    source_sequences=[source.id],
                    error=str(exc),
                )
                failures.append(
                    FailedMessage(
                        source_message_id=source.id,
                        source_telegram_message_id=source.message_id,
                        source_chat_id=source.chat_id,
                        error=str(exc),
                        status="uncertain",
                    )
                )
                uncertain += 1
                return UnitOutcome("uncertain", success, failed, uncertain)
            except PermanentSendError as exc:
                ok = await self._mark_units(
                    [history_id],
                    job,
                    status="failed",
                    destination_id=destination.id,
                    source_sequences=[source.id],
                    error=str(exc),
                )
                failures.append(
                    FailedMessage(
                        source_message_id=source.id,
                        source_telegram_message_id=source.message_id,
                        source_chat_id=source.chat_id,
                        error=str(exc),
                        status="failed",
                    )
                )
                if not ok:
                    return UnitOutcome("lease_lost", success, failed, uncertain)
                failed += 1
                continue
            except TransientSendError as exc:
                await self._mark_units(
                    [history_id],
                    job,
                    status=MSG_RETRY,
                    destination_id=destination.id,
                    source_sequences=[],
                    error=str(exc),
                    advance_cursor=False,
                )
                return UnitOutcome("retry", success, failed, uncertain)

            ok = await self._mark_units(
                [history_id],
                job,
                status="sent",
                destination_id=destination.id,
                source_sequences=[source.id],
                sent_message_ids=[sent_message_id],
                success_sequence=source.id,
            )
            if not ok:
                return UnitOutcome("lease_lost", success, failed, uncertain)
            success += 1
        return UnitOutcome("sent", success, failed, uncertain)

    async def _mark_units(
        self,
        history_ids: list[int],
        job: ClaimedJob,
        *,
        status: str,
        destination_id: int,
        source_sequences: list[int],
        error: str | None = None,
        sent_message_ids: list[int | None] | None = None,
        advance_cursor: bool = True,
        success_sequence: int | None = None,
    ) -> bool:
        return await queue_repo.mark_history_units(
            self._db,
            history_ids=history_ids,
            lease_token=job.lease_token,
            status=status,
            destination_id=destination_id,
            source_sequences=source_sequences,
            error=error,
            sent_message_ids=sent_message_ids,
            advance_cursor=advance_cursor,
            success_sequence=success_sequence,
        )

    async def _send_with_pacing(
        self,
        call: Callable[[], Awaitable[Any]],
        *,
        job: ClaimedJob,
        report: WorkerReport,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self._settings.max_send_retries + 1):
            if not self._limits.can_attempt(report.send_attempts):
                raise _AttemptLimitReached
            remaining_wall = (
                self._limits.wall_soft_deadline_seconds
                - (self._monotonic() - self._run_started)
            )
            if remaining_wall <= 1.0:
                raise _DeadlineReached("Worker wall deadline reached before send")
            permit = await self._pacer.acquire(timeout_seconds=remaining_wall)
            if permit is None:
                raise _DeadlineReached("Timed out acquiring the pacing permit")
            authorization: SendAuthorization | None = None
            try:
                if not await self._pacer.renew(permit):
                    raise _PermitLost
                authorization = await self._pacer.authorize(
                    permit, job_id=job.id, lease_token=job.lease_token
                )
                if authorization is None:
                    raise _PermitLost
                deadline = self._pacer.local_deadline(authorization)
                if not self._pacer.local_deadline_ok(deadline):
                    raise _PermitLost
                report.send_attempts += 1
                try:
                    return await asyncio.wait_for(
                        call(), timeout=self._limits.send_timeout_seconds
                    )
                except TimeoutError as exc:
                    raise AmbiguousSendError("Telegram send timed out") from exc
            except _PermitLost:
                last_error = _PermitLost()
                if attempt >= self._settings.max_send_retries:
                    raise TransientSendError("Pacing permit lost before send") from None
                continue
            except FloodWaitError as exc:
                last_error = exc
                if attempt >= self._settings.max_send_retries:
                    break
                remaining_wall = (
                    self._limits.wall_soft_deadline_seconds
                    - (self._monotonic() - self._run_started)
                )
                wait_for = min(3600.0, float(exc.retry_after) + 1.0)
                if wait_for >= remaining_wall:
                    raise TransientSendError(
                        f"Flood wait of {wait_for}s exceeds the remaining run budget"
                    ) from exc
                logger.warning("Telegram flood wait: sleeping %ss", wait_for)
                await self._sleep(wait_for)
            except AmbiguousSendError:
                raise
            except TransientSendError as exc:
                last_error = exc
                if attempt >= self._settings.max_send_retries:
                    break
                backoff = self._settings.send_retry_backoff_seconds * attempt
                remaining_wall = (
                    self._limits.wall_soft_deadline_seconds
                    - (self._monotonic() - self._run_started)
                )
                if backoff > 0:
                    if backoff >= remaining_wall:
                        raise TransientSendError(
                            "Retry backoff exceeds the remaining run budget"
                        ) from exc
                    await self._sleep(backoff)
            finally:
                if authorization is not None:
                    await self._pacer.release(
                        permit, send_started_at=authorization.send_started_at
                    )
                else:
                    await self._pacer.release(
                        permit, send_started_at=_now_datetime()
                    )
        raise TransientSendError(
            f"Transient failure after {self._settings.max_send_retries} attempts: "
            f"{last_error}"
        )

    async def _heartbeat_loop(
        self,
        job: ClaimedJob,
        stop_event: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._limits.heartbeat_interval_seconds,
                )
            except TimeoutError:
                pass
            else:
                return
            result = await heartbeat_with_retry(
                lambda: self._heartbeat_once(job),
                sleep=self._sleep,
                transient_error=(TransientDBError,),
            )
            if result != HEARTBEAT_OK:
                lease_lost.set()
                return

    async def _heartbeat_once(self, job: ClaimedJob) -> bool:
        try:
            return await queue_repo.heartbeat_job(
                self._db,
                job_id=job.id,
                lease_token=job.lease_token,
                lease_seconds=self._limits.lease_seconds,
            )
        except Exception as exc:
            if is_transient_db_error(exc):
                raise TransientDBError(str(exc)) from exc
            raise

    async def _finish_run(
        self,
        run_id: str,
        status: str,
        success_count: int,
        failed_count: int,
        uncertain_count: int,
        skipped_count: int,
        error: str | None,
        *,
        job: ClaimedJob,
    ) -> bool:
        with contextlib.suppress(Exception):
            return await publishing_repo.finish_run(
                self._db,
                run_id=run_id,
                status=status,
                success_count=success_count,
                failed_count=failed_count,
                uncertain_count=uncertain_count,
                skipped_count=skipped_count,
                error=error,
                job_id=job.id,
                lease_token=job.lease_token,
            )
        return False

    async def _pause_reason(self, destination_id: int) -> str | None:
        if await scheduler_repo.is_globally_paused(self._db):
            return "Publishing is globally paused"
        destination = await destinations_repo.get_by_id(self._db, destination_id)
        if destination is None:
            return "Destination does not exist"
        if not destination.enabled:
            return "Destination is disabled"
        if destination.paused:
            return "Destination is paused"
        return None


def _run_status(
    *,
    outcome: str,
    success_count: int,
    failed_count: int,
    uncertain_count: int,
) -> str:
    if outcome == "lease_lost":
        return RUN_INCOMPLETE
    if outcome in {
        "retry",
        "paused",
        "hard_deadline",
        "wall_soft_deadline",
        "work_soft_deadline",
        "attempt_limit",
    }:
        return RUN_INCOMPLETE
    if uncertain_count or failed_count:
        return RUN_COMPLETED_WITH_ERRORS
    return RUN_COMPLETED


def _is_unique_violation(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "23505" or type(exc).__name__ == "UniqueViolationError"


def _now_datetime() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def recover_expired_job(
    db: Database, *, retry_backoff_seconds: float = RETRY_BACKOFF_SECONDS
) -> RecoveryOutcome | None:
    """Convenience wrapper used by the worker entrypoint and tests."""

    return await queue_repo.recover_expired_job(
        db, retry_backoff_seconds=retry_backoff_seconds
    )

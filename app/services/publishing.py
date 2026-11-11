"""Core publishing engine.

Design guarantees:
* one independent cursor per destination
* one batch per destination per scheduled date
* history is reserved before the API call and committed after it
* completed messages are never selected again unless the cursor is manually
  rewound outside this application
* transient failures stop the batch without advancing the cursor
* permanent per-message failures are recorded and reported, then skipped
* media groups are copied with copyMessages in one call when possible
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from app.config import Settings
from app.constants import (
    MAX_MEDIA_GROUP_SIZE,
    MSG_RETRY,
    RUN_COMPLETED,
    RUN_COMPLETED_WITH_ERRORS,
    RUN_DISABLED,
    RUN_FAILED,
    RUN_INCOMPLETE,
    RUN_NO_MESSAGES,
    RUN_PAUSED,
    RUN_RUNNING,
)
from app.db.database import Database
from app.db.models import Candidate, Destination, FailedMessage, RunResult
from app.db.repositories import destinations as destinations_repo
from app.db.repositories import publishing as publishing_repo
from app.db.repositories import scheduler as scheduler_repo
from app.services.rate_limiter import RateLimiter
from app.services.sender import (
    FloodWaitError,
    PermanentSendError,
    TransientSendError,
)
from app.utils.time import utc_now_iso

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkUnit:
    candidates: list[Candidate]
    is_album: bool


class PublishingService:
    def __init__(
        self,
        db: Database,
        sender: object,
        settings: Settings,
        *,
        sleep: Callable[[float], asyncio.Future] | None = None,
    ) -> None:
        self._db = db
        self._sender = sender
        self._settings = settings
        self._sleep = sleep or asyncio.sleep
        self._limiter = RateLimiter(settings.send_delay_seconds, sleep=self._sleep)
        self._lock = asyncio.Lock()

    def _supports_albums(self) -> bool:
        return callable(getattr(self._sender, "copy_messages", None))

    async def publish_destination(
        self,
        destination_id: int,
        *,
        trigger: str = "manual",
        max_messages: int | None = None,
    ) -> RunResult:
        async with self._lock:
            return await self._publish_locked(
                destination_id, trigger=trigger, max_messages=max_messages
            )

    async def publish_all(
        self, *, trigger: str = "manual", destination_ids: list[int] | None = None
    ) -> list[RunResult]:
        if destination_ids is None:
            destinations = await destinations_repo.list_destinations(self._db, enabled_only=True)
            destination_ids = [destination.id for destination in destinations]
        results: list[RunResult] = []
        for destination_id in destination_ids:
            results.append(await self.publish_destination(destination_id, trigger=trigger))
        return results

    async def _publish_locked(
        self, destination_id: int, *, trigger: str, max_messages: int | None
    ) -> RunResult:
        run_id = uuid.uuid4().hex
        started_at = utc_now_iso()
        destination = await destinations_repo.get_by_id(self._db, destination_id)
        if destination is None:
            return RunResult(
                run_id=run_id,
                destination_id=destination_id,
                destination_chat_id=0,
                destination_title=None,
                trigger=trigger,
                status=RUN_DISABLED,
                error="Destination does not exist",
                started_at=started_at,
                finished_at=started_at,
            )
        if not destination.enabled:
            return RunResult(
                run_id=run_id,
                destination_id=destination.id,
                destination_chat_id=destination.chat_id,
                destination_title=destination.title,
                trigger=trigger,
                status=RUN_DISABLED,
                error="Destination is disabled",
                started_at=started_at,
                finished_at=started_at,
            )

        pause_reason = await self._pause_reason(destination_id)
        if pause_reason:
            return RunResult(
                run_id=run_id,
                destination_id=destination.id,
                destination_chat_id=destination.chat_id,
                destination_title=destination.title,
                trigger=trigger,
                status=RUN_PAUSED,
                error=pause_reason,
                started_at=started_at,
                finished_at=started_at,
            )

        queue = await destinations_repo.get_queue(self._db, destination.id)
        if queue is None:
            await destinations_repo.ensure_queue(self._db, destination.id)
            queue = await destinations_repo.get_queue(self._db, destination.id)
        next_sequence = queue.next_sequence if queue else 1

        limit = destination.batch_size
        if max_messages is not None:
            limit = max(1, min(limit, max_messages))
        pending = await publishing_repo.pending_count(
            self._db, destination_id=destination.id, next_sequence=next_sequence
        )
        planned = min(limit, pending)

        await publishing_repo.create_run(
            self._db,
            run_id=run_id,
            destination_id=destination.id,
            trigger=trigger,
            status=RUN_RUNNING,
            planned_count=planned,
        )

        result = RunResult(
            run_id=run_id,
            destination_id=destination.id,
            destination_chat_id=destination.chat_id,
            destination_title=destination.title,
            trigger=trigger,
            status=RUN_INCOMPLETE,
            planned_count=planned,
            started_at=started_at,
        )

        candidates = await publishing_repo.list_candidates(
            self._db,
            destination_id=destination.id,
            next_sequence=next_sequence,
            limit=limit + MAX_MEDIA_GROUP_SIZE,
        )
        if not candidates:
            result.status = RUN_NO_MESSAGES
            result.finished_at = utc_now_iso()
            await self._finish(result)
            return result

        processed = 0
        since_sleep = 0
        aborted = False
        try:
            for unit in self._build_units(candidates):
                if processed >= limit:
                    break
                size = len(unit.candidates)
                if processed + size > limit and processed > 0:
                    break

                pause_reason = await self._pause_reason(destination.id)
                if pause_reason:
                    result.status = RUN_PAUSED
                    result.error = pause_reason
                    logger.info(
                        "Publishing paused for destination %s: %s",
                        destination.chat_id,
                        pause_reason,
                    )
                    break

                if (
                    unit.is_album
                    and self._supports_albums()
                    and all(item.history_status in (None, MSG_RETRY) for item in unit.candidates)
                ):
                    outcome = await self._process_album(unit, destination, run_id, result)
                    if outcome == "aborted":
                        aborted = True
                        break
                    if outcome == "fallback":
                        outcome = await self._process_individual(unit, destination, run_id, result)
                        if outcome == "aborted":
                            aborted = True
                            break
                else:
                    outcome = await self._process_individual(unit, destination, run_id, result)
                    if outcome == "aborted":
                        aborted = True
                        break

                processed += size
                since_sleep += size
                if (
                    since_sleep >= self._settings.publish_chunk_size
                    and processed < limit
                    and self._settings.chunk_delay_seconds > 0
                ):
                    logger.debug(
                        "Chunk pause of %ss after %s messages",
                        self._settings.chunk_delay_seconds,
                        since_sleep,
                    )
                    await self._sleep(self._settings.chunk_delay_seconds)
                    since_sleep = 0

            if aborted:
                result.status = RUN_INCOMPLETE
            elif result.status == RUN_PAUSED:
                pass
            elif result.failed_count or result.uncertain_count:
                result.status = RUN_COMPLETED_WITH_ERRORS
            else:
                result.status = RUN_COMPLETED
        except Exception as exc:
            logger.exception(
                "Unexpected publishing failure for destination %s", destination.chat_id
            )
            result.status = RUN_FAILED
            result.error = f"Unexpected publishing failure: {exc}"

        result.finished_at = utc_now_iso()
        await self._finish(result)
        final_queue = await destinations_repo.get_queue(self._db, destination.id)
        result.next_sequence = final_queue.next_sequence if final_queue else None
        logger.info(
            "Publishing run %s destination=%s status=%s ok=%s failed=%s uncertain=%s",
            run_id,
            destination.chat_id,
            result.status,
            result.success_count,
            result.failed_count,
            result.uncertain_count,
        )
        return result

    async def _finish(self, result: RunResult) -> None:
        try:
            await publishing_repo.finish_run(
                self._db,
                run_id=result.run_id,
                status=result.status,
                success_count=result.success_count,
                failed_count=result.failed_count,
                uncertain_count=result.uncertain_count,
                skipped_count=result.skipped_count,
                error=result.error,
            )
        except Exception:
            logger.exception("Failed to persist publishing run %s", result.run_id)

    async def _process_album(
        self,
        unit: WorkUnit,
        destination: Destination,
        run_id: str,
        result: RunResult,
    ) -> str:
        sources = [item.source for item in unit.candidates]
        if len({source.chat_id for source in sources}) != 1:
            return "fallback"
        try:
            _, history_ids = await publishing_repo.reserve_album_for_send(
                self._db,
                destination_id=destination.id,
                sources=sources,
                run_id=run_id,
            )
        except publishing_repo.AlbumBlocked:
            return "fallback"

        try:
            sent_ids = await self._send_with_retries(
                lambda: self._sender.copy_messages(  # type: ignore[attr-defined]
                    chat_id=destination.chat_id,
                    from_chat_id=sources[0].chat_id,
                    message_ids=[source.message_id for source in sources],
                )
            )
        except PermanentSendError as exc:
            await publishing_repo.mark_failed_many(
                self._db,
                destination_id=destination.id,
                history_ids=history_ids,
                source_sequences=[source.id for source in sources],
                error=str(exc),
            )
            result.failed_count += len(sources)
            for source in sources:
                result.failures.append(
                    FailedMessage(
                        source_message_id=source.id,
                        source_telegram_message_id=source.message_id,
                        source_chat_id=source.chat_id,
                        error=str(exc),
                        status="failed",
                    )
                )
            return "continue"
        except TransientSendError as exc:
            await publishing_repo.mark_retry_many(self._db, history_ids=history_ids, error=str(exc))
            result.error = f"Transient album failure: {exc}"
            logger.warning("Transient album failure: %s", exc)
            return "aborted"
        except Exception as exc:
            await publishing_repo.mark_retry_many(self._db, history_ids=history_ids, error=str(exc))
            result.error = f"Unexpected album failure: {exc}"
            logger.exception("Unexpected album failure")
            return "aborted"

        sent_ids = list(sent_ids or [])
        if len(sent_ids) < len(sources):
            sent_ids.extend([None] * (len(sources) - len(sent_ids)))
        await publishing_repo.mark_sent_many(
            self._db,
            destination_id=destination.id,
            history_ids=history_ids,
            source_sequences=[source.id for source in sources],
            sent_message_ids=sent_ids[: len(sources)],
        )
        result.success_count += len(sources)
        return "continue"

    async def _process_individual(
        self,
        unit: WorkUnit,
        destination: Destination,
        run_id: str,
        result: RunResult,
    ) -> str:
        for candidate in unit.candidates:
            source = candidate.source
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
                result.skipped_count += 1
                continue
            if reservation == "uncertain":
                await publishing_repo.advance_past(
                    self._db,
                    destination_id=destination.id,
                    source_sequence=source.id,
                )
                result.uncertain_count += 1
                result.failures.append(
                    FailedMessage(
                        source_message_id=source.id,
                        source_telegram_message_id=source.message_id,
                        source_chat_id=source.chat_id,
                        error="Stale sending reservation",
                        status="uncertain",
                    )
                )
                continue
            if history_id is None:
                result.error = "Could not reserve publishing history row"
                return "aborted"

            try:
                sent_message_id = await self._send_with_retries(
                    lambda source=source: self._sender.copy_message(  # type: ignore[attr-defined]
                        chat_id=destination.chat_id,
                        from_chat_id=source.chat_id,
                        message_id=source.message_id,
                    )
                )
            except PermanentSendError as exc:
                await publishing_repo.mark_failed(
                    self._db,
                    destination_id=destination.id,
                    history_id=history_id,
                    source_sequence=source.id,
                    error=str(exc),
                )
                result.failed_count += 1
                result.failures.append(
                    FailedMessage(
                        source_message_id=source.id,
                        source_telegram_message_id=source.message_id,
                        source_chat_id=source.chat_id,
                        error=str(exc),
                        status="failed",
                    )
                )
                logger.warning(
                    "Permanent copy failure destination=%s source=%s: %s",
                    destination.chat_id,
                    source.id,
                    exc,
                )
                continue
            except TransientSendError as exc:
                await publishing_repo.mark_retry(self._db, history_id=history_id, error=str(exc))
                result.error = f"Transient failure, batch will resume: {exc}"
                logger.warning(
                    "Transient copy failure destination=%s source=%s: %s",
                    destination.chat_id,
                    source.id,
                    exc,
                )
                return "aborted"
            except Exception as exc:
                await publishing_repo.mark_retry(self._db, history_id=history_id, error=str(exc))
                result.error = f"Unexpected failure, batch will resume: {exc}"
                logger.exception("Unexpected copy failure")
                return "aborted"

            await publishing_repo.mark_sent(
                self._db,
                destination_id=destination.id,
                history_id=history_id,
                source_sequence=source.id,
                sent_message_id=sent_message_id,
            )
            result.success_count += 1
        return "continue"

    async def _send_with_retries(self, call):
        last_error: Exception | None = None
        for attempt in range(1, self._settings.max_send_retries + 1):
            await self._limiter.wait()
            try:
                return await call()
            except FloodWaitError as exc:
                last_error = exc
                if attempt >= self._settings.max_send_retries:
                    break
                wait_for = min(3600, exc.retry_after + 1)
                logger.warning("Telegram flood wait: sleeping %ss", wait_for)
                await self._sleep(wait_for)
            except TransientSendError as exc:
                last_error = exc
                if attempt >= self._settings.max_send_retries:
                    break
                backoff = self._settings.send_retry_backoff_seconds * attempt
                if backoff > 0:
                    await self._sleep(backoff)
        if isinstance(last_error, FloodWaitError):
            raise TransientSendError(
                f"Flood wait persisted after retries: {last_error}"
            ) from last_error
        raise TransientSendError(
            f"Transient failure after {self._settings.max_send_retries} attempts: {last_error}"
        )

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

    @staticmethod
    def _build_units(candidates: list[Candidate]) -> list[WorkUnit]:
        units: list[WorkUnit] = []
        index = 0
        while index < len(candidates):
            candidate = candidates[index]
            group_id = candidate.source.media_group_id
            if not group_id:
                units.append(WorkUnit([candidate], False))
                index += 1
                continue

            group = [candidate]
            lookahead = index + 1
            while (
                lookahead < len(candidates)
                and len(group) < MAX_MEDIA_GROUP_SIZE
                and candidates[lookahead].source.media_group_id == group_id
                and candidates[lookahead].source.chat_id == candidate.source.chat_id
            ):
                group.append(candidates[lookahead])
                lookahead += 1

            eligible = len(group) > 1 and all(
                item.history_status in (None, MSG_RETRY) for item in group
            )
            units.append(WorkUnit(group, eligible))
            index = lookahead
        return units

"""Phase 2 request creation facade.

The service refuses SQLite because the queue tables exist only in PostgreSQL.
SQLite keeps its Phase 1 inline publishing path.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.db.database import Database
from app.db.repositories import queue as queue_repo
from app.db.repositories.queue import RequestCreation


class QueueUnsupportedBackend(RuntimeError):
    """The Phase 2 queue is PostgreSQL-only."""


def select_catchup_dates(
    last_date: date | None,
    target: date,
    *,
    max_cycles: int = 7,
) -> list[date]:
    """Return missed dates oldest-first, capped at ``max_cycles``.

    A brand-new installation with no recorded cycle returns the target only; the
    existing startup policy still decides whether to enqueue it.
    """

    if last_date is None:
        missed = [target]
    else:
        missed = []
        cursor = last_date + timedelta(days=1)
        while cursor <= target:
            missed.append(cursor)
            cursor += timedelta(days=1)
    return missed[: max(0, max_cycles)]


class QueueRequestService:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _require_postgres(self) -> None:
        if self._db.backend != "postgres":
            raise QueueUnsupportedBackend(
                "Phase 2 request queue requires the PostgreSQL backend"
            )

    async def create_manual_request(
        self,
        *,
        request_type: str = "manual",
        requested_by: int | None = None,
        destination_id: int | None = None,
    ) -> RequestCreation:
        self._require_postgres()
        return await queue_repo.create_manual_request(
            self._db,
            request_type=request_type,
            requested_by=requested_by,
            destination_id=destination_id,
        )

    async def create_scheduled_request(
        self,
        *,
        request_type: str,
        schedule_date: str,
        slot: str,
        destination_id: int | None = None,
    ) -> RequestCreation:
        self._require_postgres()
        return await queue_repo.create_scheduled_request(
            self._db,
            request_type=request_type,
            schedule_date=schedule_date,
            slot=slot,
            destination_id=destination_id,
        )

    async def enqueue_catchup(
        self,
        *,
        last_date: date | None,
        target: date,
        slot: str,
        max_cycles: int = 7,
    ) -> list[RequestCreation]:
        self._require_postgres()
        results: list[RequestCreation] = []
        for missed_date in select_catchup_dates(
            last_date, target, max_cycles=max_cycles
        ):
            results.append(
                await self.create_scheduled_request(
                    request_type="catchup",
                    schedule_date=missed_date.isoformat(),
                    slot=slot,
                )
            )
        return results

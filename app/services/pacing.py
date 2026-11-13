"""Global Telegram pacing backed by one PostgreSQL permit row.

Guarantee
---------
While a worker is not suspended arbitrarily, at most one worker holds a valid
permit at a time. A worker only sends after:

1. acquiring the permit,
2. renewing it,
3. a final database ownership check, and
4. a local monotonic deadline check with a one-second safety margin.

The release step spaces send starts by at least ``SEND_SPACING_SECONDS`` using
the database time recorded immediately before the socket call.

Known limitation
----------------
There is no atomic "validate permit and perform the network send" operation,
and Telegram has no idempotency key. If a worker is suspended after the final
ownership/deadline check but before the socket write, its permit may expire or
be transferred. Another worker can then acquire the permit and send before the
suspended worker resumes. That residual post-check window is unavoidable for a
database-only permit; this module does not claim an absolute no-overlap
guarantee under arbitrary process suspension.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from app.db.database import Database

SEND_SPACING_SECONDS = 3.05
PERMIT_TTL_SECONDS = 25
PERMIT_SAFETY_MARGIN_SECONDS = 1.0
ACQUIRE_POLL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class PacingPermit:
    token: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SendAuthorization:
    remaining_seconds: float
    send_started_at: datetime


class PermitUnavailable(RuntimeError):
    """The worker could not acquire a pacing permit before its deadline."""


class PostgresPacer:
    """Shared PostgreSQL permit implementation of the worker pacing protocol."""

    def __init__(
        self,
        db: Database,
        *,
        spacing_seconds: float = SEND_SPACING_SECONDS,
        ttl_seconds: int = PERMIT_TTL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if spacing_seconds < 0:
            raise ValueError("spacing_seconds must be non-negative")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._db = db
        self.spacing_seconds = spacing_seconds
        self.ttl_seconds = ttl_seconds
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic

    async def acquire(self, *, timeout_seconds: float) -> PacingPermit | None:
        """Poll for the shared permit until it is acquired or the deadline passes."""

        deadline = self._monotonic() + max(0.0, timeout_seconds)
        token = uuid.uuid4()
        while True:
            row = await self._db.fetchone(
                """
                UPDATE telegram_rate_limit
                SET permit_token = ?,
                    permit_expires_at = clock_timestamp() + make_interval(secs => ?),
                    updated_at = clock_timestamp()
                WHERE id = 1
                  AND next_allowed_at <= clock_timestamp()
                  AND (
                      permit_token IS NULL
                      OR permit_expires_at < clock_timestamp()
                  )
                RETURNING permit_token, permit_expires_at
                """,
                (token, self.ttl_seconds),
            )
            if row is not None:
                return PacingPermit(
                    token=row["permit_token"], expires_at=row["permit_expires_at"]
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return None
            await self._sleep(min(ACQUIRE_POLL_SECONDS, remaining))

    async def renew(self, permit: PacingPermit) -> bool:
        row = await self._db.fetchone(
            """
            UPDATE telegram_rate_limit
            SET permit_expires_at = clock_timestamp() + make_interval(secs => ?),
                updated_at = clock_timestamp()
            WHERE id = 1
              AND permit_token = ?
              AND permit_expires_at >= clock_timestamp()
            RETURNING permit_token
            """,
            (self.ttl_seconds, permit.token),
        )
        return row is not None

    async def authorize(
        self,
        permit: PacingPermit,
        *,
        job_id: str,
        lease_token: uuid.UUID,
    ) -> SendAuthorization | None:
        """Final ownership check; records ``send_started_at`` in the same statement."""

        row = await self._db.fetchone(
            """
            WITH permit AS (
                UPDATE telegram_rate_limit
                SET updated_at = clock_timestamp()
                WHERE id = 1
                  AND permit_token = ?
                  AND permit_expires_at >= clock_timestamp()
                RETURNING
                    EXTRACT(EPOCH FROM (permit_expires_at - clock_timestamp()))
                        AS remaining_seconds,
                    clock_timestamp() AS send_started_at
            )
            UPDATE publishing_jobs j
            SET send_started_at = permit.send_started_at,
                updated_at = clock_timestamp()
            FROM permit
            WHERE j.id = ?
              AND j.status = 'running'
              AND j.lease_token = ?
            RETURNING permit.remaining_seconds, permit.send_started_at
            """,
            (permit.token, job_id, lease_token),
        )
        if row is None:
            return None
        return SendAuthorization(
            remaining_seconds=float(row["remaining_seconds"]),
            send_started_at=row["send_started_at"],
        )

    async def release(
        self, permit: PacingPermit, *, send_started_at: datetime
    ) -> None:
        await self._db.execute(
            """
            UPDATE telegram_rate_limit
            SET next_allowed_at = GREATEST(
                    clock_timestamp(),
                    CAST(? AS timestamptz) + make_interval(secs => ?)
                ),
                permit_token = NULL,
                permit_expires_at = NULL,
                updated_at = clock_timestamp()
            WHERE id = 1
              AND permit_token = ?
            """,
            (send_started_at, self.spacing_seconds, permit.token),
        )

    def local_deadline(self, authorization: SendAuthorization) -> float:
        """Return the monotonic time after which a send must not start."""

        usable = max(0.0, authorization.remaining_seconds - PERMIT_SAFETY_MARGIN_SECONDS)
        return self._monotonic() + usable

    def local_deadline_ok(self, deadline: float) -> bool:
        return self._monotonic() < deadline

"""PostgreSQL Phase 2 request, job, link and lease persistence.

This module is PostgreSQL-only. SQLite keeps the Phase 1 inline publishing
engine and never creates these tables.

Transaction discipline
----------------------
Every public function owns a short database transaction and returns before any
network call or sleep. Request creation and job completion both lock the
destination row first (ascending ``id`` order) before touching ``publishing_jobs``
or ``publish_request_jobs``. That single lock order serialises the
"request arrives while a job is completing" race and prevents deadlocks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.db.database import Database
from app.utils.time import utc_now_iso

MANUAL_TRIGGERS = ("manual", "resume")
SCHEDULE_TRIGGERS = ("schedule", "catchup")
ACTIVE_JOB_STATUSES = ("pending", "retry", "running")
TERMINAL_REQUEST_STATUSES = ("completed", "completed_with_errors")


class _TokenMismatch(Exception):
    """Internal rollback signal for a lost history lease token."""


@dataclass(frozen=True, slots=True)
class RequestCreation:
    request_id: str
    job_ids: tuple[str, ...] = ()
    coalesced_job_ids: tuple[str, ...] = ()
    cancelled: bool = False
    skipped: bool = False
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: str
    request_id: str
    destination_id: int
    trigger: str
    schedule_date: str | None
    slot: str | None
    retry_count: int
    max_retries: int
    run_id: str | None
    lease_token: uuid.UUID


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    job_id: str
    destination_id: int
    action: str
    uncertain_messages: int = 0
    error: str | None = None


@dataclass(slots=True)
class WorkerReport:
    admitted: bool
    worker_run_id: str | None = None
    claimed_jobs: int = 0
    send_attempts: int = 0
    completed_jobs: int = 0
    retry_jobs: int = 0
    uncertain_messages: int = 0
    stopped_reason: str = "idle"
    charged_seconds: int = 0
    errors: list[str] = field(default_factory=list)


async def _lock_destination_tx(db: Database, destination_id: int) -> bool:
    row = await db.fetchone(
        "SELECT id FROM destinations WHERE id = ? FOR UPDATE",
        (destination_id,),
    )
    return row is not None


async def _lock_eligible_destinations_tx(
    db: Database, destination_id: int | None
) -> list[int]:
    """Lock eligible destination rows in ascending id order and return their ids."""

    if destination_id is None:
        rows = await db.fetchall(
            "SELECT id FROM destinations "
            "WHERE enabled = 1 AND paused = 0 ORDER BY id FOR UPDATE"
        )
    else:
        rows = await db.fetchall(
            "SELECT id FROM destinations "
            "WHERE enabled = 1 AND paused = 0 AND id = ? ORDER BY id FOR UPDATE",
            (destination_id,),
        )
    return [int(row["id"]) for row in rows]


async def create_manual_request(
    db: Database,
    *,
    request_type: str,
    requested_by: int | None,
    destination_id: int | None,
    priority: int = 10,
    max_retries: int = 5,
) -> RequestCreation:
    """Create a manual/resume request and coalesce it onto an active cycle."""

    if request_type not in MANUAL_TRIGGERS:
        raise ValueError(f"Invalid manual request type: {request_type!r}")
    request_id = uuid.uuid4().hex
    dedupe_key = f"{request_type}:{request_id}"
    inserted: list[str] = []
    destination_ids: list[int] = []

    async with db.transaction():
        destination_ids = await _lock_eligible_destinations_tx(db, destination_id)
        await db.execute(
            """
            INSERT INTO publish_requests(
                id, request_type, requested_by, destination_id, dedupe_key, status
            ) VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (request_id, request_type, requested_by, destination_id, dedupe_key),
        )

        for dest_id in destination_ids:
            job_id = uuid.uuid4().hex
            rows = await db.fetchall(
                """
                INSERT INTO publishing_jobs(
                    id, request_id, destination_id, trigger, status, priority,
                    available_at, max_retries, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, 'pending', ?, clock_timestamp(), ?,
                    clock_timestamp(), clock_timestamp()
                )
                ON CONFLICT (destination_id)
                    WHERE trigger IN ('manual', 'resume')
                      AND status IN ('pending', 'retry', 'running')
                DO NOTHING
                RETURNING id
                """,
                (job_id, request_id, dest_id, request_type, priority, max_retries),
            )
            if rows:
                inserted.append(str(rows[0]["id"]))

        linked: set[str] = set(inserted)
        if destination_ids:
            rows = await db.fetchall(
                """
                INSERT INTO publish_request_jobs(request_id, job_id)
                SELECT ?, j.id
                FROM publishing_jobs j
                WHERE j.destination_id = ANY(?)
                  AND j.trigger IN ('manual', 'resume')
                  AND j.status IN ('pending', 'retry', 'running')
                ON CONFLICT DO NOTHING
                RETURNING job_id
                """,
                (request_id, destination_ids),
            )
            linked |= {str(row["job_id"]) for row in rows}

        if linked:
            await _recompute_request_tx(db, request_id)
            status_row = await db.fetchone(
                "SELECT status FROM publish_requests WHERE id = ?", (request_id,)
            )
            status = str(status_row["status"]) if status_row else "pending"
        else:
            await _cancel_request_tx(db, request_id, "no_eligible_destinations")
            status = "cancelled"

    coalesced = tuple(sorted(linked - set(inserted)))
    return RequestCreation(
        request_id=request_id,
        job_ids=tuple(sorted(linked)),
        coalesced_job_ids=coalesced,
        cancelled=status == "cancelled",
        status=status,
    )


async def create_scheduled_request(
    db: Database,
    *,
    request_type: str,
    schedule_date: str,
    slot: str,
    destination_id: int | None,
    priority: int = 20,
    max_retries: int = 5,
) -> RequestCreation:
    """Create an idempotent scheduled/catch-up request.

    Jobs are deduplicated by ``cycle:date:slot:destination``. Only jobs whose
    date and slot match are linked, so another day's job can never attach.
    """

    if request_type not in SCHEDULE_TRIGGERS:
        raise ValueError(f"Invalid scheduled request type: {request_type!r}")
    request_id = uuid.uuid4().hex
    dedupe_key = f"{request_type}:{schedule_date}:{slot}"

    async with db.transaction():
        destination_ids = await _lock_eligible_destinations_tx(db, destination_id)
        row = await db.fetchone(
            """
            INSERT INTO publish_requests(
                id, request_type, requested_by, destination_id,
                schedule_date, slot, dedupe_key, status
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, 'pending')
            ON CONFLICT (dedupe_key) DO UPDATE
            SET updated_at = publish_requests.updated_at
            RETURNING id, status, (xmax = 0) AS created
            """,
            (
                request_id,
                request_type,
                destination_id,
                schedule_date,
                slot,
                dedupe_key,
            ),
        )
        if row is None:
            raise RuntimeError("Failed to upsert publish request")
        request_id = str(row["id"])
        status = str(row["status"])
        existed = not bool(row["created"])
        if status in TERMINAL_REQUEST_STATUSES:
            return RequestCreation(
                request_id=request_id, cancelled=False, skipped=True, status=status
            )

        inserted: list[str] = []
        for dest_id in destination_ids:
            job_id = uuid.uuid4().hex
            job_key = f"cycle:{schedule_date}:{slot}:{dest_id}"
            rows = await db.fetchall(
                """
                INSERT INTO publishing_jobs(
                    id, request_id, destination_id, trigger, schedule_date, slot,
                    dedupe_key, status, priority, available_at, max_retries,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'pending', ?, clock_timestamp(), ?,
                    clock_timestamp(), clock_timestamp()
                )
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING id
                """,
                (
                    job_id,
                    request_id,
                    dest_id,
                    request_type,
                    schedule_date,
                    slot,
                    job_key,
                    priority,
                    max_retries,
                ),
            )
            if rows:
                inserted.append(str(rows[0]["id"]))

        linked: set[str] = set(inserted)
        if destination_ids:
            await db.execute(
                """
                INSERT INTO publish_request_jobs(request_id, job_id)
                SELECT ?, j.id
                FROM publishing_jobs j
                WHERE j.destination_id = ANY(?)
                  AND j.schedule_date = ?
                  AND j.slot = ?
                ON CONFLICT DO NOTHING
                """,
                (request_id, destination_ids, schedule_date, slot),
            )
            # A duplicate scheduled trigger may arrive when the links already
            # exist. Read them back so the existing request is not cancelled.
            rows = await db.fetchall(
                """
                SELECT l.job_id
                FROM publish_request_jobs l
                JOIN publishing_jobs j ON j.id = l.job_id
                WHERE l.request_id = ?
                  AND j.destination_id = ANY(?)
                  AND j.schedule_date = ?
                  AND j.slot = ?
                """,
                (request_id, destination_ids, schedule_date, slot),
            )
            linked |= {str(row["job_id"]) for row in rows}

        if linked:
            await _recompute_request_tx(db, request_id)
            status_row = await db.fetchone(
                "SELECT status FROM publish_requests WHERE id = ?", (request_id,)
            )
            status = str(status_row["status"]) if status_row else "pending"
        else:
            await _cancel_request_tx(db, request_id, "no_eligible_destinations")
            status = "cancelled"

    return RequestCreation(
        request_id=request_id,
        job_ids=tuple(sorted(linked)),
        coalesced_job_ids=tuple(sorted(linked - set(inserted))),
        cancelled=status == "cancelled",
        skipped=existed,
        status=status,
    )


async def _cancel_request_tx(db: Database, request_id: str, reason: str) -> None:
    await db.execute(
        """
        UPDATE publish_requests SET
            status = 'cancelled', cancelled_reason = ?,
            finished_at = clock_timestamp(), updated_at = clock_timestamp()
        WHERE id = ?
        """,
        (reason, request_id),
    )


async def _recompute_request_tx(db: Database, request_id: str) -> None:
    await db.execute(
        """
        WITH counts AS (
            SELECT
                l.request_id,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE j.status = 'running') AS running,
                COUNT(*) FILTER (WHERE j.status IN ('pending', 'retry')) AS queued,
                COUNT(*) FILTER (
                    WHERE j.status IN ('completed', 'completed_with_errors')
                ) AS completed,
                COUNT(*) FILTER (WHERE j.status = 'failed') AS failed,
                COUNT(*) FILTER (WHERE j.status = 'cancelled') AS cancelled,
                COUNT(*) FILTER (WHERE j.status = 'completed_with_errors') AS with_errors
            FROM publish_request_jobs l
            JOIN publishing_jobs j ON j.id = l.job_id
            WHERE l.request_id = ?
            GROUP BY l.request_id
        )
        UPDATE publish_requests r
        SET planned_jobs = c.total,
            completed_jobs = c.completed,
            failed_jobs = c.failed,
            cancelled_jobs = c.cancelled,
            status = CASE
                WHEN c.total = 0 THEN 'cancelled'
                WHEN c.running > 0 THEN 'processing'
                WHEN c.queued > 0 THEN 'pending'
                WHEN c.failed = c.total THEN 'failed'
                WHEN c.cancelled = c.total THEN 'cancelled'
                WHEN c.failed > 0 OR c.cancelled > 0
                  OR c.with_errors > 0 THEN 'completed_with_errors'
                ELSE 'completed'
            END,
            finished_at = CASE
                WHEN c.running = 0 AND c.queued = 0 THEN clock_timestamp()
                ELSE NULL
            END,
            updated_at = clock_timestamp()
        FROM counts c
        WHERE r.id = c.request_id
        """,
        (request_id,),
    )


async def _recompute_requests_for_job_tx(db: Database, job_id: str) -> None:
    await db.execute(
        """
        WITH affected AS (
            SELECT DISTINCT request_id
            FROM publish_request_jobs
            WHERE job_id = ?
        ),
        counts AS (
            SELECT
                l.request_id,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE j.status = 'running') AS running,
                COUNT(*) FILTER (WHERE j.status IN ('pending', 'retry')) AS queued,
                COUNT(*) FILTER (
                    WHERE j.status IN ('completed', 'completed_with_errors')
                ) AS completed,
                COUNT(*) FILTER (WHERE j.status = 'failed') AS failed,
                COUNT(*) FILTER (WHERE j.status = 'cancelled') AS cancelled,
                COUNT(*) FILTER (WHERE j.status = 'completed_with_errors') AS with_errors
            FROM publish_request_jobs l
            JOIN publishing_jobs j ON j.id = l.job_id
            WHERE l.request_id IN (SELECT request_id FROM affected)
            GROUP BY l.request_id
        )
        UPDATE publish_requests r
        SET planned_jobs = c.total,
            completed_jobs = c.completed,
            failed_jobs = c.failed,
            cancelled_jobs = c.cancelled,
            status = CASE
                WHEN c.total = 0 THEN 'cancelled'
                WHEN c.running > 0 THEN 'processing'
                WHEN c.queued > 0 THEN 'pending'
                WHEN c.failed = c.total THEN 'failed'
                WHEN c.cancelled = c.total THEN 'cancelled'
                WHEN c.failed > 0 OR c.cancelled > 0
                  OR c.with_errors > 0 THEN 'completed_with_errors'
                ELSE 'completed'
            END,
            finished_at = CASE
                WHEN c.running = 0 AND c.queued = 0 THEN clock_timestamp()
                ELSE NULL
            END,
            updated_at = clock_timestamp()
        FROM counts c
        WHERE r.id = c.request_id
        """,
        (job_id,),
    )


async def claim_next_job(
    db: Database,
    *,
    worker_id: str,
    lease_token: uuid.UUID,
    lease_seconds: int = 180,
) -> ClaimedJob | None:
    """Atomically claim one runnable job for a destination with no running job."""

    row = await db.fetchone(
        """
        WITH candidate AS (
            SELECT j.id
            FROM publishing_jobs j
            JOIN destinations d ON d.id = j.destination_id
            WHERE j.status IN ('pending', 'retry')
              AND j.available_at <= clock_timestamp()
              AND d.enabled = 1
              AND d.paused = 0
              AND EXISTS (
                  SELECT 1
                  FROM publish_request_jobs l
                  JOIN publish_requests r ON r.id = l.request_id
                  WHERE l.job_id = j.id
                    AND r.status IN ('pending', 'processing')
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM publishing_jobs active
                  WHERE active.destination_id = j.destination_id
                    AND active.status = 'running'
              )
            ORDER BY
                j.priority ASC,
                j.available_at ASC,
                j.schedule_date ASC NULLS LAST,
                j.created_at ASC
            LIMIT 1
            FOR UPDATE OF j SKIP LOCKED
        )
        UPDATE publishing_jobs j
        SET status = 'running',
            claimed_by = ?,
            lease_token = ?,
            claimed_at = clock_timestamp(),
            heartbeat_at = clock_timestamp(),
            lease_expires_at = clock_timestamp() + make_interval(secs => ?),
            send_started_at = NULL,
            updated_at = clock_timestamp()
        FROM candidate
        WHERE j.id = candidate.id
        RETURNING
            j.id, j.request_id, j.destination_id, j.trigger,
            j.schedule_date, j.slot, j.retry_count, j.max_retries,
            j.run_id, j.lease_token
        """,
        (worker_id, lease_token, lease_seconds),
    )
    if row is None:
        return None
    return ClaimedJob(
        id=str(row["id"]),
        request_id=str(row["request_id"]),
        destination_id=int(row["destination_id"]),
        trigger=str(row["trigger"]),
        schedule_date=row["schedule_date"],
        slot=row["slot"],
        retry_count=int(row["retry_count"]),
        max_retries=int(row["max_retries"]),
        run_id=row["run_id"],
        lease_token=row["lease_token"],
    )


async def attach_run(
    db: Database,
    *,
    job_id: str,
    lease_token: uuid.UUID,
    run_id: str,
) -> bool:
    row = await db.fetchone(
        """
        UPDATE publishing_jobs
        SET run_id = ?, updated_at = clock_timestamp()
        WHERE id = ? AND status = 'running' AND lease_token = ?
        RETURNING id
        """,
        (run_id, job_id, lease_token),
    )
    return row is not None


async def heartbeat_job(
    db: Database,
    *,
    job_id: str,
    lease_token: uuid.UUID,
    lease_seconds: int = 180,
) -> bool:
    row = await db.fetchone(
        """
        UPDATE publishing_jobs
        SET heartbeat_at = clock_timestamp(),
            lease_expires_at = clock_timestamp() + make_interval(secs => ?),
            updated_at = clock_timestamp()
        WHERE id = ?
          AND status = 'running'
          AND lease_token = ?
        RETURNING id
        """,
        (lease_seconds, job_id, lease_token),
    )
    return row is not None


async def finish_job(
    db: Database,
    *,
    job_id: str,
    lease_token: uuid.UUID,
    status: str,
    outcome: str | None = None,
    error: str | None = None,
) -> bool:
    """Finish a running job with the lease token as a fence."""

    async with db.transaction():
        job = await db.fetchone(
            "SELECT destination_id FROM publishing_jobs WHERE id = ? AND lease_token = ?",
            (job_id, lease_token),
        )
        if job is None:
            return False
        await _lock_destination_tx(db, int(job["destination_id"]))
        rows = await db.fetchall(
            """
            UPDATE publishing_jobs SET
                status = ?, outcome = ?, last_error = ?,
                finished_at = clock_timestamp(),
                claimed_by = NULL, lease_token = NULL, lease_expires_at = NULL,
                updated_at = clock_timestamp()
            WHERE id = ? AND status = 'running' AND lease_token = ?
            RETURNING id
            """,
            (status, outcome, _short(error), job_id, lease_token),
        )
        if not rows:
            return False
        await _recompute_requests_for_job_tx(db, job_id)
    return True


async def release_job(
    db: Database,
    *,
    job_id: str,
    lease_token: uuid.UUID,
    error: str | None,
    available_after_seconds: float = 0.0,
    increment_retry: bool = True,
) -> str:
    """Release a running job back to ``retry`` (or ``failed`` at the limit)."""

    status = "retry"
    async with db.transaction():
        job = await db.fetchone(
            "SELECT destination_id, retry_count, max_retries "
            "FROM publishing_jobs WHERE id = ? AND lease_token = ?",
            (job_id, lease_token),
        )
        if job is None:
            return "lease_lost"
        await _lock_destination_tx(db, int(job["destination_id"]))
        retry_count = int(job["retry_count"])
        max_retries = int(job["max_retries"])
        next_retry = retry_count + (1 if increment_retry else 0)
        if increment_retry and next_retry > max_retries:
            status = "failed"
            rows = await db.fetchall(
                """
                UPDATE publishing_jobs SET
                    status = 'failed', retry_count = ?, outcome = 'retry_exhausted',
                    last_error = ?, finished_at = clock_timestamp(),
                    claimed_by = NULL, lease_token = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE id = ? AND status = 'running' AND lease_token = ?
                RETURNING id
                """,
                (next_retry, _short(error), job_id, lease_token),
            )
        else:
            rows = await db.fetchall(
                """
                UPDATE publishing_jobs SET
                    status = 'retry', retry_count = ?,
                    available_at = clock_timestamp() + make_interval(secs => ?),
                    last_error = ?, run_id = NULL,
                    claimed_by = NULL, lease_token = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE id = ? AND status = 'running' AND lease_token = ?
                RETURNING id
                """,
                (next_retry, max(0.0, available_after_seconds), _short(error), job_id, lease_token),
            )
        if not rows:
            return "lease_lost"
        await _recompute_requests_for_job_tx(db, job_id)
    return status


async def recover_expired_job(
    db: Database,
    *,
    retry_backoff_seconds: float = 60.0,
) -> RecoveryOutcome | None:
    """Recover one job whose lease expired.

    If a token-fenced ``sending`` history row exists, the send is ambiguous:
    the row becomes ``uncertain``, the cursor advances past it and the job is
    completed with errors. Otherwise the job is safe to retry.
    """

    async with db.transaction():
        candidate = await db.fetchone(
            """
            SELECT id, destination_id
            FROM publishing_jobs
            WHERE status = 'running'
              AND lease_expires_at <= clock_timestamp()
            ORDER BY lease_expires_at
            LIMIT 1
            """
        )
        if candidate is None:
            return None
        job_id = str(candidate["id"])
        destination_id = int(candidate["destination_id"])
        await _lock_destination_tx(db, destination_id)
        job = await db.fetchone(
            """
            SELECT id, destination_id, run_id, retry_count, max_retries, lease_token
            FROM publishing_jobs
            WHERE id = ?
              AND status = 'running'
              AND lease_expires_at <= clock_timestamp()
            FOR UPDATE
            """,
            (job_id,),
        )
        if job is None or job["lease_token"] is None:
            return None
        lease_token = job["lease_token"]
        run_id = job["run_id"]
        now_text = utc_now_iso()

        if run_id:
            sending = await db.fetchall(
                """
                UPDATE publishing_history SET
                    status = 'uncertain',
                    error = 'Worker lease expired before delivery outcome was recorded',
                    lease_token = NULL,
                    updated_at = ?
                WHERE run_id = ?
                  AND status = 'sending'
                  AND lease_token = ?
                RETURNING id, source_message_id
                """,
                (now_text, run_id, lease_token),
            )
        else:
            sending = []

        if sending:
            max_sequence = max(int(row["source_message_id"]) for row in sending)
            await db.execute(
                """
                UPDATE publishing_queues SET
                    next_sequence = MAX(next_sequence, ?),
                    updated_at = ?
                WHERE destination_id = ?
                """,
                (max_sequence + 1, now_text, destination_id),
            )
            await db.execute(
                """
                UPDATE publishing_runs SET
                    status = 'completed_with_errors',
                    uncertain_count = uncertain_count + ?,
                    finished_at = ?,
                    error = COALESCE(error, ?)
                WHERE id = ?
                """,
                (
                    len(sending),
                    now_text,
                    "Worker lease expired with an ambiguous send",
                    run_id,
                ),
            )
            await db.execute(
                """
                UPDATE publishing_jobs SET
                    status = 'completed_with_errors', outcome = 'uncertain',
                    last_error = ?, finished_at = clock_timestamp(),
                    claimed_by = NULL, lease_token = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (
                    "Worker lease expired with an ambiguous send",
                    job_id,
                    lease_token,
                ),
            )
            await _recompute_requests_for_job_tx(db, job_id)
            return RecoveryOutcome(
                job_id=job_id,
                destination_id=destination_id,
                action="uncertain",
                uncertain_messages=len(sending),
                error="Ambiguous send after lease expiry",
            )

        next_retry = int(job["retry_count"]) + 1
        if next_retry > int(job["max_retries"]):
            new_status = "failed"
            await db.execute(
                """
                UPDATE publishing_jobs SET
                    status = 'failed', retry_count = ?, outcome = 'retry_exhausted',
                    last_error = ?, finished_at = clock_timestamp(),
                    claimed_by = NULL, lease_token = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (
                    next_retry,
                    "Worker lease expired; retry limit reached",
                    job_id,
                    lease_token,
                ),
            )
        else:
            new_status = "retry"
            await db.execute(
                """
                UPDATE publishing_jobs SET
                    status = 'retry', retry_count = ?,
                    available_at = clock_timestamp() + make_interval(secs => ?),
                    last_error = ?, run_id = NULL,
                    claimed_by = NULL, lease_token = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (
                    next_retry,
                    max(0.0, retry_backoff_seconds),
                    "Worker lease expired before any send",
                    job_id,
                    lease_token,
                ),
            )

        if run_id:
            await db.execute(
                """
                UPDATE publishing_runs SET
                    status = 'incomplete', finished_at = ?,
                    error = COALESCE(error, ?)
                WHERE id = ? AND status = 'running'
                """,
                (now_text, "Worker lease expired before any send", run_id),
            )
        await _recompute_requests_for_job_tx(db, job_id)
        return RecoveryOutcome(
            job_id=job_id,
            destination_id=destination_id,
            action=new_status,
            error="Worker lease expired before any send",
        )


async def tag_history_units(
    db: Database,
    *,
    history_ids: list[int],
    unit_id: uuid.UUID,
    lease_token: uuid.UUID,
) -> bool:
    """Attach the unit and lease tokens to freshly reserved ``sending`` rows."""

    if not history_ids:
        return False
    result = await db.execute(
        """
        UPDATE publishing_history
        SET unit_id = ?, lease_token = ?
        WHERE id = ANY(?) AND status = 'sending'
        """,
        (unit_id, lease_token, history_ids),
    )
    return result.rowcount == len(history_ids)


async def mark_history_units(
    db: Database,
    *,
    history_ids: list[int],
    lease_token: uuid.UUID,
    status: str,
    destination_id: int,
    source_sequences: list[int],
    error: str | None = None,
    sent_message_ids: list[int | None] | None = None,
    advance_cursor: bool = True,
    success_sequence: int | None = None,
) -> bool:
    """Token-fenced terminal/retry update for one message or an album.

    Returns ``False`` when any row no longer belongs to ``lease_token``; the
    caller must treat that as a lost lease and must not advance the cursor.
    """

    if not history_ids:
        return False
    now = utc_now_iso()
    sent_ids = list(sent_message_ids or [])
    try:
        async with db.transaction():
            for index, history_id in enumerate(history_ids):
                if status == "sent":
                    sent_id = sent_ids[index] if index < len(sent_ids) else None
                    row = await db.fetchone(
                        """
                        UPDATE publishing_history SET
                            status = ?, error = ?, updated_at = ?,
                            sent_message_id = ?, lease_token = NULL
                        WHERE id = ? AND status = 'sending' AND lease_token = ?
                        RETURNING id
                        """,
                        (status, _short(error), now, sent_id, history_id, lease_token),
                    )
                else:
                    row = await db.fetchone(
                        """
                        UPDATE publishing_history SET
                            status = ?, error = ?, updated_at = ?,
                            lease_token = NULL
                        WHERE id = ? AND status = 'sending' AND lease_token = ?
                        RETURNING id
                        """,
                        (status, _short(error), now, history_id, lease_token),
                    )
                if row is None:
                    raise _TokenMismatch
            if advance_cursor and source_sequences:
                next_sequence = max(source_sequences) + 1
                if status == "sent":
                    await db.execute(
                        """
                        UPDATE publishing_queues SET
                            next_sequence = MAX(next_sequence, ?),
                            last_success_sequence = MAX(
                                COALESCE(last_success_sequence, 0), ?
                            ),
                            updated_at = ?
                        WHERE destination_id = ?
                        """,
                        (next_sequence, success_sequence, now, destination_id),
                    )
                else:
                    await db.execute(
                        """
                        UPDATE publishing_queues SET
                            next_sequence = MAX(next_sequence, ?),
                            updated_at = ?
                        WHERE destination_id = ?
                        """,
                        (next_sequence, now, destination_id),
                    )
    except _TokenMismatch:
        return False
    return True


async def get_job(db: Database, job_id: str) -> dict[str, Any] | None:
    return await db.fetchone("SELECT * FROM publishing_jobs WHERE id = ?", (job_id,))


async def get_request(db: Database, request_id: str) -> dict[str, Any] | None:
    return await db.fetchone("SELECT * FROM publish_requests WHERE id = ?", (request_id,))


async def count_active_manual_jobs(db: Database, destination_id: int) -> int:
    row = await db.fetchone(
        """
        SELECT COUNT(*) AS count
        FROM publishing_jobs
        WHERE destination_id = ?
          AND trigger IN ('manual', 'resume')
          AND status IN ('pending', 'retry', 'running')
        """,
        (destination_id,),
    )
    return int(row["count"]) if row else 0


def _short(value: str | None, limit: int = 1000) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 3] + "..."

"""Conservative GitHub Actions worker accounting.

Rules implemented here:

* A completed run is charged with per-run ceiling rounding:
  ``ceil(actual_seconds / 60) * 60``. Never round after monthly aggregation.
* A running or abandoned run is charged at most 3600 seconds (60 minutes),
  matching the workflow timeout rather than the 58-minute worker timeout.
* Admission reserves a full 3600 seconds before starting a run. Existing
  ``running`` rows also hold their full 3600-second reservation, so overlapping
  invocations cannot over-admit. A run is only admitted when
  ``committed_seconds + 3600 * (running_runs + 1) <= 114000`` (1900 minutes).
* When a month is tight, manual/catch-up work is reduced before scheduled work.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.db.database import Database

SETUP_SECONDS = 120
ACCOUNTING_CAP_SECONDS = 3600
INTERNAL_MONTHLY_CAP_SECONDS = 114_000
PLATFORM_MONTHLY_ALLOWANCE_SECONDS = 120_000
REAPER_THRESHOLD_SECONDS = 4200
NORMAL_RUN_MINUTES = 52
ABANDONED_RUN_MINUTES = 60
MANUAL_ALLOWANCE_MINUTES = 120
INTERNAL_CAP_MINUTES = INTERNAL_MONTHLY_CAP_SECONDS // 60

# Stable namespace for the admission advisory lock. PostgreSQL's hashtext is
# used so every worker contends on the same bigint key without storing state.
_ADMISSION_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(?))"


def month_key(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC).strftime("%Y-%m")


def charged_seconds(actual_seconds: float, *, cap: int = ACCOUNTING_CAP_SECONDS) -> int:
    """Per-run ceiling rounding, capped at the platform timeout."""

    if actual_seconds <= 0:
        return 0
    rounded = math.ceil(actual_seconds / 60.0) * 60
    return min(int(rounded), cap)


def split_charge(charge_seconds: int) -> tuple[int, int]:
    """Return ``(setup_seconds, work_seconds)`` for a total charged amount."""

    total = max(0, min(int(charge_seconds), ACCOUNTING_CAP_SECONDS))
    setup = min(SETUP_SECONDS, total)
    return setup, total - setup


@dataclass(frozen=True, slots=True)
class BudgetRow:
    days: int
    retry_rate: float
    scheduled_minutes: int
    retry_minutes: int
    manual_minutes: int
    abandoned_uplift_minutes: int
    ledger_minutes: int
    within_internal_cap: bool


def model_budget(
    *,
    days: int,
    retry_rate: float = 0.0,
    manual_minutes: int = MANUAL_ALLOWANCE_MINUTES,
    abandoned_runs: int = 4,
) -> BudgetRow:
    """Model a month using the approved conservative assumptions.

    Scheduled work is 52 minutes per day. The abandoned uplift is 8 minutes per
    abandoned run (60 charged instead of 52). Manual work is reduced first when
    the ledger would exceed the 1900-minute internal cap.
    """

    if days not in (30, 31):
        raise ValueError("Budget model supports 30-day and 31-day months")
    if retry_rate < 0:
        raise ValueError("retry_rate must be non-negative")
    scheduled = days * NORMAL_RUN_MINUTES
    retry = round(scheduled * retry_rate)
    uplift = abandoned_runs * (ABANDONED_RUN_MINUTES - NORMAL_RUN_MINUTES)
    fixed = scheduled + retry + uplift
    allowed_manual = max(0, INTERNAL_CAP_MINUTES - fixed)
    used_manual = min(manual_minutes, allowed_manual)
    ledger = fixed + used_manual
    return BudgetRow(
        days=days,
        retry_rate=retry_rate,
        scheduled_minutes=scheduled,
        retry_minutes=retry,
        manual_minutes=used_manual,
        abandoned_uplift_minutes=uplift,
        ledger_minutes=ledger,
        within_internal_cap=ledger <= INTERNAL_CAP_MINUTES,
    )


async def monthly_used_seconds(db: Database, key: str) -> int:
    row = await db.fetchone(
        """
        SELECT COALESCE(SUM(
            CASE
                WHEN status = 'running'
                THEN LEAST(
                    EXTRACT(EPOCH FROM (clock_timestamp() - started_at))::int,
                    ?
                )
                ELSE setup_seconds + work_seconds
            END
        ), 0) AS used_seconds
        FROM worker_runs
        WHERE month_key = ?
        """,
        (ACCOUNTING_CAP_SECONDS, key),
    )
    return int(row["used_seconds"]) if row else 0


async def admit_worker_run(
    db: Database,
    *,
    worker_id: str,
    key: str | None = None,
    reserve_seconds: int = ACCOUNTING_CAP_SECONDS,
) -> str | None:
    """Atomically reserve a full platform timeout and insert a running row."""

    if reserve_seconds < 1:
        raise ValueError("reserve_seconds must be positive")
    # A running row is always charged at the platform cap, so admission must
    # reserve at least that much. A caller can reserve more, never less,
    # otherwise the cap could be exceeded by the row this call inserts.
    reservation = max(reserve_seconds, ACCOUNTING_CAP_SECONDS)
    month = key or month_key()
    run_id = uuid.uuid4().hex
    async with db.transaction():
        await db.fetchone(_ADMISSION_LOCK_SQL, (f"worker_runs_admission:{month}",))
        row = await db.fetchone(
            """
            SELECT COALESCE(SUM(
                CASE
                    WHEN status = 'running' THEN ?
                    ELSE setup_seconds + work_seconds
                END
            ), 0) AS used_seconds
            FROM worker_runs
            WHERE month_key = ?
            """,
            (ACCOUNTING_CAP_SECONDS, month),
        )
        used = int(row["used_seconds"]) if row else 0
        if used + reservation > INTERNAL_MONTHLY_CAP_SECONDS:
            return None
        await db.execute(
            """
            INSERT INTO worker_runs(
                id, worker_id, status, month_key, started_at,
                setup_seconds, work_seconds, claimed_jobs
            ) VALUES (?, ?, 'running', ?, clock_timestamp(), ?, 0, 0)
            """,
            (run_id, worker_id, month, SETUP_SECONDS),
        )
    return run_id


async def finish_worker_run(
    db: Database,
    *,
    run_id: str,
    actual_seconds: float,
    status: str = "completed",
    claimed_jobs: int = 0,
    error: str | None = None,
) -> int:
    """Finish a run and store its per-run ceiling-rounded charge."""

    if status not in {"completed", "failed", "abandoned"}:
        raise ValueError(f"Invalid worker run status: {status!r}")
    total = (
        ACCOUNTING_CAP_SECONDS
        if status == "abandoned"
        else charged_seconds(actual_seconds)
    )
    setup, work = split_charge(total)
    await db.execute(
        """
        UPDATE worker_runs SET
            status = ?, finished_at = clock_timestamp(),
            setup_seconds = ?, work_seconds = ?, claimed_jobs = ?,
            error = ?
        WHERE id = ? AND status = 'running'
        """,
        (status, setup, work, claimed_jobs, _short(error), run_id),
    )
    return total


async def reap_abandoned_runs(db: Database) -> int:
    """Charge running rows older than the platform timeout plus grace at 3600s."""

    result = await db.execute(
        """
        UPDATE worker_runs SET
            status = 'abandoned',
            finished_at = LEAST(
                clock_timestamp(),
                started_at + make_interval(secs => ?)
            ),
            work_seconds = GREATEST(
                LEAST(
                    EXTRACT(EPOCH FROM (clock_timestamp() - started_at))::int,
                    ?
                ) - setup_seconds,
                0
            ),
            error = COALESCE(error, 'Worker run abandoned; charged at platform timeout')
        WHERE status = 'running'
          AND started_at < clock_timestamp() - make_interval(secs => ?)
        """,
        (ACCOUNTING_CAP_SECONDS, ACCOUNTING_CAP_SECONDS, REAPER_THRESHOLD_SECONDS),
    )
    return result.rowcount


def _short(value: str | None, limit: int = 1000) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 3] + "..."

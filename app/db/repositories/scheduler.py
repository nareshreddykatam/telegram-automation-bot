"""Scheduler state and per-date scheduled-run claims."""

from __future__ import annotations

from app.constants import SCHED_COMPLETED, SCHED_FAILED, SCHED_RUNNING
from app.db.database import Database
from app.utils.time import utc_now_iso


async def get_state(db: Database, key: str) -> str | None:
    row = await db.fetchone("SELECT value FROM scheduler_state WHERE key = ?", (key,))
    return str(row["value"]) if row else None


async def set_state(db: Database, key: str, value: str) -> None:
    await db.execute(
        """
        INSERT INTO scheduler_state(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utc_now_iso()),
    )


async def is_globally_paused(db: Database) -> bool:
    return (await get_state(db, "publishing_paused")) == "1"


async def set_globally_paused(db: Database, paused: bool, updated_by: int | None = None) -> None:
    await set_state(db, "publishing_paused", "1" if paused else "0")
    if updated_by is not None:
        await set_state(db, "publishing_paused_by", str(updated_by))


async def get_last_cycle_date(db: Database) -> str | None:
    return await get_state(db, "last_cycle_date")


async def set_last_cycle_date(db: Database, date_value: str) -> None:
    await set_state(db, "last_cycle_date", date_value)
    await set_state(db, "last_cycle_completed_at", utc_now_iso())


async def claim_scheduled_run(
    db: Database,
    *,
    schedule_date: str,
    destination_id: int,
    trigger: str,
) -> bool:
    """Claim a destination's batch for a schedule date.

    A completed claim is never executed again. A failed/running claim (for
    example after a crash or a pause) may be retried.
    """

    now = utc_now_iso()
    async with db.transaction():
        row = await db.fetchone(
            """
            SELECT status FROM scheduled_runs
            WHERE schedule_date = ? AND destination_id = ?
            """,
            (schedule_date, destination_id),
        )
        if row is not None and row["status"] == SCHED_COMPLETED:
            return False
        if row is None:
            await db.execute(
                """
                INSERT INTO scheduled_runs(
                    schedule_date, destination_id, run_id, trigger, status,
                    created_at, updated_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?)
                """,
                (schedule_date, destination_id, trigger, SCHED_RUNNING, now, now),
            )
        else:
            await db.execute(
                """
                UPDATE scheduled_runs SET
                    trigger = ?, status = ?, error = NULL, updated_at = ?
                WHERE schedule_date = ? AND destination_id = ?
                """,
                (trigger, SCHED_RUNNING, now, schedule_date, destination_id),
            )
    return True


async def finish_scheduled_run(
    db: Database,
    *,
    schedule_date: str,
    destination_id: int,
    run_id: str,
    success: bool,
    error: str | None = None,
) -> None:
    await db.execute(
        """
        UPDATE scheduled_runs SET
            run_id = ?, status = ?, error = ?, updated_at = ?
        WHERE schedule_date = ? AND destination_id = ?
        """,
        (
            run_id,
            SCHED_COMPLETED if success else SCHED_FAILED,
            error,
            utc_now_iso(),
            schedule_date,
            destination_id,
        ),
    )


async def reset_running_scheduled_runs(db: Database) -> int:
    cursor = await db.execute(
        """
        UPDATE scheduled_runs SET
            status = ?, error = COALESCE(error, 'Reset after bot restart'),
            updated_at = ?
        WHERE status = ?
        """,
        (SCHED_FAILED, utc_now_iso(), SCHED_RUNNING),
    )
    return cursor.rowcount


async def get_scheduled_run(db: Database, schedule_date: str, destination_id: int) -> str | None:
    row = await db.fetchone(
        """
        SELECT status FROM scheduled_runs
        WHERE schedule_date = ? AND destination_id = ?
        """,
        (schedule_date, destination_id),
    )
    return str(row["status"]) if row else None

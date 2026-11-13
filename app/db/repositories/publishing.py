"""Publishing queue, run and history persistence.

The cursor and the per-message history rows are updated in the same SQLite
transaction. A history row is reserved as ``sending`` *before* the Telegram API
call. If the process dies after Telegram accepted the copy but before the row is
marked sent, restart recovery marks it ``uncertain`` and moves the cursor past
it. That deliberately favors at-most-once delivery over possible duplicates,
while still surfacing the uncertain message in reports and /history.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass

from app.constants import (
    MSG_FAILED,
    MSG_RETRY,
    MSG_SENDING,
    MSG_SENT,
    MSG_UNCERTAIN,
    RUN_INCOMPLETE,
)
from app.db.database import Database
from app.db.models import Candidate, FailedMessage, PublishingRun, SourceMessage
from app.utils.time import utc_now_iso


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    uncertain_messages: int
    aborted_runs: int
    affected_destinations: int


async def create_run(
    db: Database,
    *,
    run_id: str,
    destination_id: int,
    trigger: str,
    status: str,
    planned_count: int,
) -> None:
    await db.execute(
        """
        INSERT INTO publishing_runs(
            id, destination_id, trigger, status, planned_count, started_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, destination_id, trigger, status, planned_count, utc_now_iso()),
    )


async def finish_run(
    db: Database,
    *,
    run_id: str,
    status: str,
    success_count: int,
    failed_count: int,
    uncertain_count: int,
    skipped_count: int,
    error: str | None = None,
    job_id: str | None = None,
    lease_token: uuid.UUID | None = None,
) -> bool:
    """Finish a run, optionally fenced by the owning job's lease token.

    The Phase 2 worker passes its job id and lease token. A worker that lost
    its lease must not overwrite the run state that lease recovery already
    finalised, so the update only applies while that exact job still owns the
    run. SQLite/Phase 1 callers pass neither and keep the historical UPDATE.
    """

    sql = """
        UPDATE publishing_runs SET
            status = ?, success_count = ?, failed_count = ?, uncertain_count = ?,
            skipped_count = ?, finished_at = ?, error = ?
        WHERE id = ?
        """
    params: list[object] = [
        status,
        success_count,
        failed_count,
        uncertain_count,
        skipped_count,
        utc_now_iso(),
        _short(error),
        run_id,
    ]
    if job_id is not None and lease_token is not None:
        sql += (
            " AND EXISTS ("
            " SELECT 1 FROM publishing_jobs"
            " WHERE id = ? AND run_id = ? AND status = 'running'"
            " AND lease_token = ?"
            ")"
        )
        params.extend((job_id, run_id, lease_token))
    cursor = await db.execute(sql, tuple(params))
    return cursor.rowcount > 0


async def get_run(db: Database, run_id: str) -> PublishingRun | None:
    row = await db.fetchone(
        """
        SELECT r.*, d.title AS destination_title, d.chat_id AS destination_chat_id
        FROM publishing_runs r
        JOIN destinations d ON d.id = r.destination_id
        WHERE r.id = ?
        """,
        (run_id,),
    )
    return PublishingRun.from_row(row) if row else None


async def list_runs(
    db: Database, *, destination_id: int | None = None, limit: int = 10
) -> list[PublishingRun]:
    if destination_id is None:
        rows = await db.fetchall(
            """
            SELECT r.*, d.title AS destination_title, d.chat_id AS destination_chat_id
            FROM publishing_runs r
            JOIN destinations d ON d.id = r.destination_id
            ORDER BY r.started_at DESC, r.rowid DESC
            LIMIT ?
            """,
            (limit,),
        )
    else:
        rows = await db.fetchall(
            """
            SELECT r.*, d.title AS destination_title, d.chat_id AS destination_chat_id
            FROM publishing_runs r
            JOIN destinations d ON d.id = r.destination_id
            WHERE r.destination_id = ?
            ORDER BY r.started_at DESC, r.rowid DESC
            LIMIT ?
            """,
            (destination_id, limit),
        )
    return [PublishingRun.from_row(row) for row in rows]


async def list_candidates(
    db: Database, *, destination_id: int, next_sequence: int, limit: int
) -> list[Candidate]:
    rows = await db.fetchall(
        """
        SELECT sm.*,
               ph.id AS h_id,
               ph.status AS h_status,
               ph.attempts AS h_attempts
        FROM source_messages sm
        LEFT JOIN publishing_history ph
            ON ph.destination_id = ? AND ph.source_message_id = sm.id
        WHERE sm.id >= ?
        ORDER BY sm.id ASC
        LIMIT ?
        """,
        (destination_id, max(1, next_sequence), limit),
    )
    candidates: list[Candidate] = []
    for row in rows:
        source = SourceMessage.from_row(row)
        candidates.append(
            Candidate(
                source=source,
                history_id=int(row["h_id"]) if row["h_id"] is not None else None,
                history_status=row["h_status"],
                attempts=int(row["h_attempts"]) if row["h_attempts"] is not None else 0,
            )
        )
    return candidates


async def reserve_for_send(
    db: Database,
    *,
    destination_id: int,
    source: SourceMessage,
    run_id: str,
) -> tuple[str, int | None]:
    """Reserve one source message for this destination.

    Returns ("booked", history_id), ("duplicate", history_id) or
    ("uncertain", history_id).
    """

    now = utc_now_iso()
    async with db.transaction():
        row = await db.fetchone(
            """
            SELECT * FROM publishing_history
            WHERE destination_id = ? AND source_message_id = ?
            """,
            (destination_id, source.id),
        )
        if row is None:
            cursor = await db.execute(
                """
                INSERT INTO publishing_history(
                    run_id, destination_id, source_message_id, source_chat_id,
                    source_telegram_message_id, status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    run_id,
                    destination_id,
                    source.id,
                    source.chat_id,
                    source.message_id,
                    MSG_SENDING,
                    now,
                    now,
                ),
            )
            return "booked", int(cursor.lastrowid)

        history_id = int(row["id"])
        status = str(row["status"])
        if status == MSG_RETRY:
            await db.execute(
                """
                UPDATE publishing_history SET
                    run_id = ?, status = ?, attempts = attempts + 1,
                    error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (run_id, MSG_SENDING, now, history_id),
            )
            return "booked", history_id
        if status == MSG_SENDING:
            # A stale sending row left by an earlier process. Recovery normally
            # handles this at startup; this is a defensive fallback.
            await db.execute(
                """
                UPDATE publishing_history SET
                    status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (MSG_UNCERTAIN, "Stale sending reservation", now, history_id),
            )
            return "uncertain", history_id
        return "duplicate", history_id


async def mark_sent(
    db: Database,
    *,
    destination_id: int,
    history_id: int,
    source_sequence: int,
    sent_message_id: int | None,
) -> None:
    now = utc_now_iso()
    async with db.transaction():
        await db.execute(
            """
            UPDATE publishing_history SET
                status = ?, sent_message_id = ?, error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (MSG_SENT, sent_message_id, now, history_id),
        )
        await _advance_queue_tx(
            db,
            destination_id=destination_id,
            next_sequence=source_sequence + 1,
            success_sequence=source_sequence,
        )


async def reserve_album_for_send(
    db: Database,
    *,
    destination_id: int,
    sources: list[SourceMessage],
    run_id: str,
) -> tuple[str, list[int]]:
    """Reserve all items of a media group atomically.

    If any item already has a terminal history row, returns ("blocked", []).
    The caller falls back to individual processing, which preserves duplicate
    safety without re-sending completed items.
    """

    now = utc_now_iso()
    history_ids: list[int] = []
    async with db.transaction():
        for source in sources:
            row = await db.fetchone(
                """
                SELECT * FROM publishing_history
                WHERE destination_id = ? AND source_message_id = ?
                """,
                (destination_id, source.id),
            )
            if row is None:
                cursor = await db.execute(
                    """
                    INSERT INTO publishing_history(
                        run_id, destination_id, source_message_id, source_chat_id,
                        source_telegram_message_id, status, attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        run_id,
                        destination_id,
                        source.id,
                        source.chat_id,
                        source.message_id,
                        MSG_SENDING,
                        now,
                        now,
                    ),
                )
                history_ids.append(int(cursor.lastrowid))
            elif row["status"] == MSG_RETRY:
                await db.execute(
                    """
                    UPDATE publishing_history SET
                        run_id = ?, status = ?, attempts = attempts + 1,
                        error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (run_id, MSG_SENDING, now, int(row["id"])),
                )
                history_ids.append(int(row["id"]))
            else:
                # Roll back all reservations from this album.
                raise AlbumBlocked()
    return "booked", history_ids


class AlbumBlocked(Exception):
    """One or more album items already have a terminal publishing record."""


async def mark_sent_many(
    db: Database,
    *,
    destination_id: int,
    history_ids: list[int],
    source_sequences: list[int],
    sent_message_ids: list[int | None],
) -> None:
    now = utc_now_iso()
    async with db.transaction():
        for history_id, sent_message_id in zip(history_ids, sent_message_ids, strict=True):
            await db.execute(
                """
                UPDATE publishing_history SET
                    status = ?, sent_message_id = ?, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (MSG_SENT, sent_message_id, now, history_id),
            )
        await _advance_queue_tx(
            db,
            destination_id=destination_id,
            next_sequence=max(source_sequences) + 1,
            success_sequence=max(source_sequences),
        )


async def mark_failed_many(
    db: Database,
    *,
    destination_id: int,
    history_ids: list[int],
    source_sequences: list[int],
    error: str,
) -> None:
    now = utc_now_iso()
    async with db.transaction():
        for history_id in history_ids:
            await db.execute(
                """
                UPDATE publishing_history SET
                    status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (MSG_FAILED, _short(error), now, history_id),
            )
        await _advance_queue_tx(
            db,
            destination_id=destination_id,
            next_sequence=max(source_sequences) + 1,
            success_sequence=None,
        )


async def mark_retry_many(db: Database, *, history_ids: list[int], error: str) -> None:
    now = utc_now_iso()
    async with db.transaction():
        for history_id in history_ids:
            await db.execute(
                """
                UPDATE publishing_history SET
                    status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (MSG_RETRY, _short(error), now, history_id),
            )


async def mark_failed(
    db: Database,
    *,
    destination_id: int,
    history_id: int,
    source_sequence: int,
    error: str,
) -> None:
    now = utc_now_iso()
    async with db.transaction():
        await db.execute(
            """
            UPDATE publishing_history SET
                status = ?, error = ?, updated_at = ?
            WHERE id = ?
            """,
            (MSG_FAILED, _short(error), now, history_id),
        )
        await _advance_queue_tx(
            db,
            destination_id=destination_id,
            next_sequence=source_sequence + 1,
            success_sequence=None,
        )


async def mark_uncertain(
    db: Database,
    *,
    destination_id: int,
    history_id: int,
    source_sequence: int,
    error: str,
) -> None:
    now = utc_now_iso()
    async with db.transaction():
        await db.execute(
            """
            UPDATE publishing_history SET
                status = ?, error = ?, updated_at = ?
            WHERE id = ?
            """,
            (MSG_UNCERTAIN, _short(error), now, history_id),
        )
        await _advance_queue_tx(
            db,
            destination_id=destination_id,
            next_sequence=source_sequence + 1,
            success_sequence=None,
        )


async def mark_retry(
    db: Database,
    *,
    history_id: int,
    error: str,
) -> None:
    await db.execute(
        """
        UPDATE publishing_history SET
            status = ?, error = ?, updated_at = ?
        WHERE id = ?
        """,
        (MSG_RETRY, _short(error), utc_now_iso(), history_id),
    )


async def advance_past(db: Database, *, destination_id: int, source_sequence: int) -> None:
    async with db.transaction():
        await _advance_queue_tx(
            db,
            destination_id=destination_id,
            next_sequence=source_sequence + 1,
            success_sequence=None,
        )


async def _advance_queue_tx(
    db: Database,
    *,
    destination_id: int,
    next_sequence: int,
    success_sequence: int | None,
) -> None:
    now = utc_now_iso()
    if success_sequence is None:
        await db.execute(
            """
            UPDATE publishing_queues SET
                next_sequence = MAX(next_sequence, ?),
                updated_at = ?
            WHERE destination_id = ?
            """,
            (next_sequence, now, destination_id),
        )
    else:
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


async def pending_count(db: Database, *, destination_id: int, next_sequence: int) -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) AS count FROM source_messages WHERE id >= ?",
        (max(1, next_sequence),),
    )
    return int(row["count"]) if row else 0


async def recent_failures(
    db: Database, *, destination_id: int | None = None, limit: int = 20
) -> list[FailedMessage]:
    if destination_id is None:
        rows = await db.fetchall(
            """
            SELECT source_message_id, source_telegram_message_id, source_chat_id,
                   error, status
            FROM publishing_history
            WHERE status IN ('failed', 'uncertain')
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    else:
        rows = await db.fetchall(
            """
            SELECT source_message_id, source_telegram_message_id, source_chat_id,
                   error, status
            FROM publishing_history
            WHERE destination_id = ? AND status IN ('failed', 'uncertain')
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (destination_id, limit),
        )
    return [
        FailedMessage(
            source_message_id=int(row["source_message_id"]),
            source_telegram_message_id=int(row["source_telegram_message_id"]),
            source_chat_id=int(row["source_chat_id"]),
            error=row["error"],
            status=str(row["status"]),
        )
        for row in rows
    ]


async def recover_interrupted(db: Database) -> RecoverySummary:
    """Make crash-interrupted work explicit and advance past uncertain sends."""

    sending_rows = await db.fetchall(
        """
        SELECT id, destination_id, source_message_id
        FROM publishing_history
        WHERE status = ?
        """,
        (MSG_SENDING,),
    )
    affected: dict[int, int] = defaultdict(int)
    for row in sending_rows:
        destination_id = int(row["destination_id"])
        affected[destination_id] = max(affected[destination_id], int(row["source_message_id"]))

    now = utc_now_iso()
    async with db.transaction():
        if sending_rows:
            await db.execute(
                """
                UPDATE publishing_history SET
                    status = ?, error = ?, updated_at = ?
                WHERE status = ?
                """,
                (
                    MSG_UNCERTAIN,
                    "Bot restarted before delivery outcome was recorded",
                    now,
                    MSG_SENDING,
                ),
            )
            for destination_id, last_sequence in affected.items():
                await _advance_queue_tx(
                    db,
                    destination_id=destination_id,
                    next_sequence=last_sequence + 1,
                    success_sequence=None,
                )
        cursor = await db.execute(
            """
            UPDATE publishing_runs SET
                status = ?, finished_at = ?,
                error = COALESCE(error, 'Bot restarted while run was in progress')
            WHERE status = 'running'
            """,
            (RUN_INCOMPLETE, now),
        )
        aborted_runs = cursor.rowcount
    return RecoverySummary(
        uncertain_messages=len(sending_rows),
        aborted_runs=aborted_runs,
        affected_destinations=len(affected),
    )


def _short(value: str | None, limit: int = 1000) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 3] + "..."

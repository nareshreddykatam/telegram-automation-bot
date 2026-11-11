"""Destination and queue configuration persistence."""

from __future__ import annotations

from app.db.database import Database
from app.db.models import Destination, QueueState
from app.utils.time import utc_now_iso


async def add_destination(
    db: Database,
    *,
    chat_id: int,
    title: str | None,
    batch_size: int,
    start_sequence: int,
    created_by: int,
    auto_approve: bool = True,
) -> tuple[Destination, bool]:
    """Create a destination and its independent queue cursor.

    Re-adding an existing destination re-enables it but never rewinds or resets
    its cursor. Returns (destination, created).
    """

    now = utc_now_iso()
    async with db.transaction():
        existing = await db.fetchone("SELECT * FROM destinations WHERE chat_id = ?", (chat_id,))
        if existing is None:
            await db.execute(
                """
                INSERT INTO destinations(
                    chat_id, title, batch_size, auto_approve, enabled, paused,
                    created_at, created_by
                ) VALUES (?, ?, ?, ?, 1, 0, ?, ?)
                """,
                (
                    chat_id,
                    title,
                    batch_size,
                    1 if auto_approve else 0,
                    now,
                    created_by,
                ),
            )
            created = True
        else:
            await db.execute(
                """
                UPDATE destinations SET
                    title = COALESCE(?, title),
                    batch_size = ?,
                    auto_approve = ?,
                    enabled = 1
                WHERE chat_id = ?
                """,
                (
                    title,
                    batch_size,
                    1 if auto_approve else 0,
                    chat_id,
                ),
            )
            created = False

        await db.execute(
            """
            INSERT INTO publishing_queues(destination_id, next_sequence, updated_at)
            SELECT id, ?, ? FROM destinations WHERE chat_id = ?
            ON CONFLICT(destination_id) DO NOTHING
            """,
            (max(1, start_sequence), now, chat_id),
        )
        await db.execute(
            """
            INSERT INTO moderation_settings(chat_id, link_protection, created_at, updated_at)
            VALUES (?, 0, ?, ?)
            ON CONFLICT(chat_id) DO NOTHING
            """,
            (chat_id, now, now),
        )
    destination = await get_by_chat_id(db, chat_id)
    if destination is None:
        raise RuntimeError("Destination was not persisted")
    return destination, created


async def get_by_chat_id(db: Database, chat_id: int) -> Destination | None:
    row = await db.fetchone("SELECT * FROM destinations WHERE chat_id = ?", (chat_id,))
    return Destination.from_row(row) if row else None


async def get_by_id(db: Database, destination_id: int) -> Destination | None:
    row = await db.fetchone("SELECT * FROM destinations WHERE id = ?", (destination_id,))
    return Destination.from_row(row) if row else None


async def list_destinations(db: Database, enabled_only: bool = True) -> list[Destination]:
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = await db.fetchall(f"SELECT * FROM destinations {where} ORDER BY id")
    return [Destination.from_row(row) for row in rows]


async def list_with_queue(db: Database) -> list[tuple[Destination, QueueState | None]]:
    rows = await db.fetchall(
        """
        SELECT d.*,
               q.destination_id AS q_destination_id,
               q.next_sequence AS q_next_sequence,
               q.last_success_sequence AS q_last_success_sequence,
               q.updated_at AS q_updated_at
        FROM destinations d
        LEFT JOIN publishing_queues q ON q.destination_id = d.id
        ORDER BY d.id
        """
    )
    result: list[tuple[Destination, QueueState | None]] = []
    for row in rows:
        destination = Destination.from_row(row)
        if row["q_destination_id"] is None:
            queue = None
        else:
            queue = QueueState(
                destination_id=int(row["q_destination_id"]),
                next_sequence=int(row["q_next_sequence"]),
                last_success_sequence=(
                    int(row["q_last_success_sequence"])
                    if row["q_last_success_sequence"] is not None
                    else None
                ),
                updated_at=str(row["q_updated_at"]),
            )
        result.append((destination, queue))
    return result


async def get_queue(db: Database, destination_id: int) -> QueueState | None:
    row = await db.fetchone(
        "SELECT * FROM publishing_queues WHERE destination_id = ?", (destination_id,)
    )
    return QueueState.from_row(row) if row else None


async def ensure_queue(db: Database, destination_id: int, start_sequence: int = 1) -> None:
    await db.execute(
        """
        INSERT INTO publishing_queues(destination_id, next_sequence, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(destination_id) DO NOTHING
        """,
        (destination_id, max(1, start_sequence), utc_now_iso()),
    )


async def set_batch_size(db: Database, chat_id: int, batch_size: int) -> bool:
    cursor = await db.execute(
        "UPDATE destinations SET batch_size = ? WHERE chat_id = ?",
        (batch_size, chat_id),
    )
    return cursor.rowcount > 0


async def set_enabled(db: Database, chat_id: int, enabled: bool) -> bool:
    cursor = await db.execute(
        "UPDATE destinations SET enabled = ? WHERE chat_id = ?",
        (1 if enabled else 0, chat_id),
    )
    return cursor.rowcount > 0


async def set_paused(db: Database, chat_id: int, paused: bool) -> bool:
    cursor = await db.execute(
        "UPDATE destinations SET paused = ? WHERE chat_id = ?",
        (1 if paused else 0, chat_id),
    )
    return cursor.rowcount > 0


async def set_auto_approve(db: Database, chat_id: int, enabled: bool) -> bool:
    cursor = await db.execute(
        "UPDATE destinations SET auto_approve = ? WHERE chat_id = ?",
        (1 if enabled else 0, chat_id),
    )
    return cursor.rowcount > 0

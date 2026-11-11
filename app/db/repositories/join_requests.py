"""Join request audit log persistence."""

from __future__ import annotations

from app.constants import JOIN_FAILED, JOIN_IGNORED
from app.db.database import Database
from app.db.models import JoinRequestLog
from app.utils.time import utc_now_iso


async def log_request(
    db: Database,
    *,
    chat_id: int,
    user_id: int,
    username: str | None,
    first_name: str | None,
    chat_title: str | None,
    action: str,
    error: str | None = None,
) -> int:
    now = utc_now_iso()
    cursor = await db.execute(
        """
        INSERT INTO join_request_logs(
            chat_id, user_id, username, first_name, chat_title, action, error,
            attempts, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (chat_id, user_id, username, first_name, chat_title, action, error, now, now),
    )
    return int(cursor.lastrowid)


async def update_result(
    db: Database,
    *,
    log_id: int,
    action: str,
    error: str | None = None,
    increment_attempts: bool = False,
) -> None:
    await db.execute(
        """
        UPDATE join_request_logs SET
            action = ?, error = ?,
            attempts = attempts + ?,
            updated_at = ?
        WHERE id = ?
        """,
        (action, error, 1 if increment_attempts else 0, utc_now_iso(), log_id),
    )


async def recent_pending_or_failed(
    db: Database, *, chat_id: int | None = None, limit: int = 200
) -> list[JoinRequestLog]:
    params: list[object] = [JOIN_FAILED, JOIN_IGNORED]
    where = "WHERE action IN (?, ?)"
    if chat_id is not None:
        where += " AND chat_id = ?"
        params.append(chat_id)
    params.append(limit)
    rows = await db.fetchall(
        f"""
        SELECT * FROM join_request_logs
        {where}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return [JoinRequestLog.from_row(row) for row in rows]


async def recent_logs(
    db: Database, *, chat_id: int | None = None, limit: int = 20
) -> list[JoinRequestLog]:
    if chat_id is None:
        rows = await db.fetchall(
            "SELECT * FROM join_request_logs ORDER BY id DESC LIMIT ?", (limit,)
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM join_request_logs WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
    return [JoinRequestLog.from_row(row) for row in rows]

"""Administrator persistence."""

from __future__ import annotations

from app.db.database import Database
from app.db.models import Admin
from app.utils.time import utc_now_iso


async def seed_owners(db: Database, owner_ids: tuple[int, ...]) -> None:
    now = utc_now_iso()
    for telegram_id in owner_ids:
        await db.execute(
            """
            INSERT INTO admins(telegram_id, role, label, is_active, created_at)
            VALUES (?, 'owner', 'OWNER_IDS bootstrap', 1, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                role = 'owner',
                is_active = 1
            """,
            (telegram_id, now),
        )


async def is_admin(db: Database, telegram_id: int) -> bool:
    row = await db.fetchone(
        "SELECT 1 AS ok FROM admins WHERE telegram_id = ? AND is_active = 1",
        (telegram_id,),
    )
    return row is not None


async def get_admin(db: Database, telegram_id: int) -> Admin | None:
    row = await db.fetchone(
        "SELECT * FROM admins WHERE telegram_id = ? AND is_active = 1",
        (telegram_id,),
    )
    return Admin.from_row(row) if row else None


async def list_admins(db: Database, active_only: bool = True) -> list[Admin]:
    where = "WHERE is_active = 1" if active_only else ""
    rows = await db.fetchall(
        f"SELECT * FROM admins {where} ORDER BY role = 'owner' DESC, telegram_id"
    )
    return [Admin.from_row(row) for row in rows]


async def add_admin(
    db: Database, telegram_id: int, created_by: int, label: str | None = None
) -> None:
    await db.execute(
        """
        INSERT INTO admins(telegram_id, role, label, is_active, created_at, created_by)
        VALUES (?, 'admin', ?, 1, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            is_active = 1,
            label = COALESCE(excluded.label, admins.label)
        """,
        (telegram_id, label, utc_now_iso(), created_by),
    )


async def set_admin_active(db: Database, telegram_id: int, active: bool) -> bool:
    cursor = await db.execute(
        "UPDATE admins SET is_active = ? WHERE telegram_id = ? AND role = 'admin'",
        (1 if active else 0, telegram_id),
    )
    return cursor.rowcount > 0


async def owner_ids(db: Database) -> list[int]:
    rows = await db.fetchall(
        "SELECT telegram_id FROM admins WHERE role = 'owner' AND is_active = 1"
    )
    return [int(row["telegram_id"]) for row in rows]

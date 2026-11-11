"""Public /start and /help handlers."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import Settings
from app.db.database import Database
from app.db.repositories import admins as admins_repo

router = Router(name="public")

PUBLIC_HELP = (
    "Telegram Automation Bot\n\n"
    "This bot manages configured groups/channels, approves join requests, "
    "moderates links, and copies content from a private source channel to "
    "independent destination queues.\n\n"
    "Administrative commands are restricted to configured owner IDs.\n\n"
    "Commands: /start /help"
)

ADMIN_HELP = """
Admin commands:
/addsource <chat_id|@username> [title]
/adddest <chat_id|@username> [batch_size] [start_sequence]
/removedest <chat_id>
/setbatch <chat_id> <size>
/pause [chat_id]
/resume [chat_id]
/status
/publishnow [chat_id]
/history [chat_id] [limit]
/linkprotection <on|off> [chat_id]
/approvepending [chat_id]
/autoapprove <on|off> [chat_id]

Notes:
- Messages posted before /addsource are not indexed (Telegram does not let a
  bot fetch a channel's full history).
- A destination's cursor only moves forward. Use start_sequence in /adddest
  when different destinations should publish different ranges.
- /pause and /resume without a chat ID control all publishing. With a chat ID
  they control one destination.
- /publishnow runs in the background and sends a report when finished.
""".strip()


@router.message(Command("start"))
async def cmd_start(message: Message, db: Database, settings: Settings) -> None:
    is_admin = bool(message.from_user and await admins_repo.is_admin(db, int(message.from_user.id)))
    lines = [
        "Telegram Automation Bot is online.",
        f"Your Telegram ID: {message.from_user.id if message.from_user else 'unknown'}",
        f"Timezone: {settings.timezone}",
        (
            "Daily publishing: "
            f"{settings.publish_hour:02d}:{settings.publish_minute:02d} "
            f"{settings.timezone}"
        ),
    ]
    if is_admin:
        lines.append("You are an authorized administrator. Use /help for commands.")
    else:
        lines.append("You are not an administrator.")
    await message.answer("\n".join(lines))


@router.message(Command("help"))
async def cmd_help(message: Message, db: Database) -> None:
    is_admin = bool(message.from_user and await admins_repo.is_admin(db, int(message.from_user.id)))
    text = PUBLIC_HELP
    if is_admin:
        text = text + "\n\n" + ADMIN_HELP
    await message.answer(text)

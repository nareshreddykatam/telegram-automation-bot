# Telegram Automation and Management Bot

A production-oriented, single-worker Telegram bot built with **Python 3.11+**,
**aiogram 3**, **SQLite**, and **APScheduler**.

It manages multiple groups/channels and copies content from one or more private
source chats into destination channels/groups. Every destination has its own
cursor, batch size, pause state, and publishing history.

## What it does

- Automatically approves join requests in configured sources/destinations.
- Deletes links posted by non-administrators in groups (per-group setting).
- Indexes new source messages (photos, videos, documents, audio, text, albums).
- Copies (not forwards) content to destinations, preserving captions.
- Publishes one independent daily batch per destination at **20:00 Asia/Kolkata**.
- Persists cursor/progress, so completed messages are never selected twice.
- Recovers interrupted work after a restart and processes missed daily batches.
- Reports successful, failed, and uncertain publishing to owners.
- Exposes an owner-only Telegram admin panel.

## Architecture

```
app/
  config.py                  Environment-only configuration
  logging_config.py          Console + rotating file logging
  main.py                    Wiring, dispatcher, startup recovery, polling
  constants.py               Status values and limits
  db/
    database.py              Async SQLite wrapper, explicit transactions
    schema.py                Complete baseline migration + future migrations
    models.py                Typed rows and result objects
    repositories/            All SQL lives here
      admins.py
      sources.py
      messages.py
      destinations.py
      publishing.py          Cursor, runs, history, crash recovery
      moderation.py
      join_requests.py
      scheduler.py
  services/
    publishing.py            Queue engine, batching, albums, retries
    sender.py                aiogram copy calls -> normalized errors
    rate_limiter.py          Conservative API pacing
    moderation.py            Admin checks + link deletion
    join_requests.py         Approval + retry
    ingest.py                Source message indexing (no downloads)
    recovery.py              Startup crash recovery
    reports.py               Admin reports
    background.py            Tracked long-running tasks
  scheduler/scheduler.py     APScheduler, daily cycle, catch-up, pause/resume
  handlers/                  Telegram command/event handlers
  middlewares/               Dependency injection + admin guard
  filters/database.py        Dynamic source/moderation filters
  utils/                     Link detection, time helpers, errors
tests/                       46 automated tests (queue, recovery, duplicates, ...)
```

The handler layer never contains SQL. Services coordinate repositories and
Telegram calls. The publishing engine depends on a small sender protocol, so its
queue behavior is tested without a live Telegram token.

## Database tables

`schema_migrations`, `admins`, `source_chats`, `source_messages`,
`destinations`, `publishing_queues`, `publishing_runs`, `publishing_history`,
`moderation_settings`, `moderation_logs`, `join_request_logs`,
`scheduler_state`, `scheduled_runs`.

Key safeguards:

- `source_messages UNIQUE(chat_id, message_id)` — duplicate updates are ignored.
- `publishing_history UNIQUE(destination_id, source_message_id)` — a message is
  never intentionally copied twice to the same destination.
- A history row is reserved as `sending` **before** the API call and committed as
  `sent` after it, so a crash cannot silently re-send it.
- `publishing_queues.next_sequence` is the per-destination cursor.
- `scheduled_runs(schedule_date, destination_id)` prevents a completed daily
  batch from running again for the same date.

## Windows setup

Python 3.11 or newer is required. The project was implemented and tested on
Python 3.14.6 on Windows.

```powershell
cd "C:\Users\Naresh Reddy\Downloads\Telegram Bot"

# Create and activate a virtual environment (py launcher is the reliable
# launcher on Windows; the Store `python` alias can open the Store instead).
py -m venv .venv
.\.venv\Scripts\Activate.ps1

# If PowerShell blocks activation:
# Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

pip install -r requirements.txt
pip install -r requirements-dev.txt

Copy-Item .env.example .env
notepad .env
```

Fill in at minimum:

```dotenv
BOT_TOKEN=123456789:your-real-token-from-BotFather
OWNER_IDS=123456789
```

Then run:

```powershell
python -m app
```

On Windows PowerShell, if `python` is not recognized inside the venv, use:

```powershell
.\.venv\Scripts\python.exe -m app
```

The process is a long-running worker. It uses long polling and must stay alive.
Do **not** deploy it as a Vercel/serverless function.

## BotFather and administrator permissions

1. Open **@BotFather** in Telegram, run `/newbot`, and copy the token into `.env`.
2. Get your numeric Telegram user ID (for example from **@userinfobot**) and put
   it in `OWNER_IDS`. Only IDs in the `admins` table (seeded from `OWNER_IDS`)
   can use admin commands.
3. Add the bot to every source and destination as an **administrator**.

Required administrator rights:

| Chat | Rights |
| --- | --- |
| Private source channel | Administrator. Reading/copying channel posts requires the bot to be an admin in the channel. |
| Destination channel | Administrator with **Post Messages**. |
| Destination group | Administrator with **Post Messages**; **Invite Users** for join-request approval; **Delete Messages** for link moderation. |
| Group with link protection | Administrator with **Delete Messages**. |
| Chat with join requests | Administrator with **Invite Users via Link** / approve permission. |

If the bot is not an administrator, `/addsource` and `/adddest` refuse the
configuration. Link protection and join approval also refuse or log failures
when permissions are missing.

### Getting numeric chat IDs

Use one of these methods:

- Add **@userinfobot** to the group temporarily and read the group ID.
- Forward a channel post to **@JsonDumpBot** and read `forward_from_chat.id`.
- With the bot running, post in the chat and inspect its ID from a helper bot.

Channel and group IDs are usually negative and start with `-100`.

## Configuring the workflow

Add the source first:

```text
/addsource -1001234567890
```

Important: **only messages posted after `/addsource` are indexed.** Telegram does
not let a bot fetch the full history of a private channel. The bot never
downloads media; it stores only chat ID, message ID, captions/text, type and
album grouping.

Add destinations. The optional third argument is the starting global source
sequence:

```text
/adddest -1001111111111 100 1
/adddest -1002222222222 100 201
/adddest -1003333333333 100 501
```

This produces independent queues such as:

- Destination A: messages 1–100 today, 101–200 tomorrow.
- Destination B: messages 201–300 today, 301–400 tomorrow.
- Destination C: messages 501–600 today, 601–700 tomorrow.

The cursor starts at the value given at creation and never moves backwards
automatically. Re-adding an existing destination re-enables it but preserves
its cursor. Use `/status` to inspect cursors.

Set a different daily batch size:

```text
/setbatch -1001111111111 50
```

Publish manually (runs in the background and sends a report):

```text
/publishnow
/publishnow -1001111111111
```

## Admin commands

All commands except `/start` and `/help` are restricted to configured
administrators.

| Command | Purpose |
| --- | --- |
| `/start` | Show bot status and your Telegram ID. |
| `/help` | Show commands (admin section shown only to admins). |
| `/addsource <chat_id>` | Configure a source chat/channel. |
| `/adddest <chat_id> [batch] [start_sequence]` | Add/re-enable a destination and its independent queue. |
| `/removedest <chat_id>` | Disable a destination; history/cursor are retained. |
| `/setbatch <chat_id> <size>` | Change that destination's daily batch size. |
| `/pause [chat_id]` | Pause all publishing, or one destination. |
| `/resume [chat_id]` | Resume and trigger a catch-up batch. |
| `/status` | Sources, destinations, cursors, pending counts, pause and schedule state. |
| `/publishnow [chat_id]` | Start a manual batch in the background. |
| `/history [chat_id] [limit]` | Recent runs plus failed/uncertain messages. |
| `/linkprotection <on\|off\|status> [chat_id]` | Configure link moderation. In a group the chat ID can be omitted. |
| `/approvepending [chat_id]` | Retry join requests the bot previously logged as failed/ignored. |
| `/autoapprove <on\|off> <chat_id>` | Toggle automatic join approval for a source/destination. |

## Scheduling and recovery

- APScheduler runs at `PUBLISH_HOUR:PUBLISH_MINUTE` in `TIMEZONE`
  (default `20:00 Asia/Kolkata`).
- Each destination claims `(date, destination)` exactly once. A completed claim
  is never run again for that date.
- The per-destination cursor advances after every successful copy. If the
  process stops mid-batch, completed messages are already persisted.
- On startup:
  - `sending` history rows from a crashed process are marked `uncertain` and the
    cursor advances past them. They are **not** blindly re-sent, which prevents
    duplicates when Telegram accepted a copy before the crash.
  - interrupted runs are marked `incomplete`.
  - `running` scheduled claims are reset for retry.
  - missed daily cycles between the last completed date and the latest due date
    are processed, capped by `MAX_CATCHUP_CYCLES` (default 7). Any remaining
    backlog stays queued and publishes in later daily batches.
  - on a brand-new installation with no recorded completed cycle, startup does
    **not** auto-publish. Nothing is sent until the first scheduled time,
    `/publishnow`, or an explicit `/resume`.
- A transient error (network, server, flood wait) stops the batch **without**
  advancing past the failed message. The next run resumes at that message.
- A permanent error (protected/unsupported content, missing source message,
  missing permission) is recorded as `failed`, reported, and the cursor advances
  so the rest of the batch can continue. It is never silently skipped.
- `/pause` takes effect before the next message; an in-flight Telegram call is
  allowed to finish. `/resume` triggers catch-up logic.

## Moderation behavior

- Every message in a group with link protection enabled is checked.
- Administrators, anonymous admins, channel senders, and bot senders are exempt.
- If administrator status cannot be verified (API error), the message is **not**
  deleted.
- Detection covers `http/https/ftp/tg/mailto`, `www.`, Telegram links, bare
  domains with a TLD allow-list, emails, URL/text-link entities, and common
  obfuscations such as `example[.]com`.
- Every delete attempt is written to `moderation_logs` with success/failure.

## Join requests

- Incoming `chat_join_request` updates for configured sources/destinations are
  approved when auto-approval is enabled.
- Success and failure are logged in `join_request_logs`.
- Telegram does **not** expose an API to list all pending join requests.
  `/approvepending` can only retry requests the bot observed and logged. Requests
  created while the bot was offline are not discoverable.
- The bot must be an administrator with the appropriate invite permission.

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check app tests
```

The 46 tests cover:

- schema/migrations, idempotent message indexing, transaction rollback
- queue progression across days
- independent destination cursors (1–100, 201–300, 501–600)
- duplicate prevention after a cursor rewind
- crash recovery of an in-flight copy without re-sending
- transient failure resume at the failed message
- permanent failure reporting and continuation
- media-group album copying and album-aware batch boundaries
- scheduled date idempotency, pause, catch-up across missed days,
  and first-startup no-auto-publish behavior
- flood-wait/retry handling
- link detection and moderation (admin exemption)
- join request approval, failure logging and retry
- caption/album metadata preservation and unsupported content handling
- configuration/timezone validation

Tests use a fake Telegram sender. **No live Telegram API call is claimed as
tested.** Validate once with a private test channel before production use.

## Running as a persistent Windows worker

### Option A: NSSM (recommended)

1. Download NSSM and install the bot as a Windows service.
2. Application path: `C:\...\Telegram Bot\.venv\Scripts\python.exe`
3. Arguments: `-m app`
4. Startup directory: `C:\...\Telegram Bot`
5. Set the service to restart on failure. Keep `data\` and `.env` on persistent
   storage.

### Option B: Task Scheduler

- Trigger: **At startup** (and optionally repeat every 5 minutes with
  "If the task is already running, do not start a new instance").
- Action: start `.venv\Scripts\python.exe` with `-m app`.
- Start in the project directory.
- Do not enable "Stop the task if it runs longer than..." for a worker.

### Linux systemd (for a VPS)

```ini
[Unit]
Description=Telegram Automation Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/telegram-bot
ExecStart=/opt/telegram-bot/.venv/bin/python -m app
Restart=always
RestartSec=5
EnvironmentFile=/opt/telegram-bot/.env

[Install]
WantedBy=multi-user.target
```

Use a persistent volume/directory for `DATABASE_PATH` and `LOG_FILE`.

## PostgreSQL migration path

SQLite is the only backend implemented and tested in this version. The code is
structured for a contained migration:

- All SQL is in `app/db/repositories/`; handlers/services are backend-agnostic.
- `app/db/database.py` is the only connection/transaction implementation.
- Migrations are numbered in `app/db/schema.py`.

To add PostgreSQL:

1. Implement an `AsyncpgDatabase` with the same methods (`execute`, `fetchone`,
   `fetchall`, `transaction`, `migrate`) using `asyncpg.Pool`.
2. Convert `?` placeholders to `$1, $2, ...` in one adapter layer.
3. Translate the baseline DDL: `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL`,
   `INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`,
   `MAX(a,b)` → `GREATEST(a,b)`, and ISO text timestamps → `timestamptz`
   (or keep ISO text for a minimal-change first step).
4. Add a new numbered migration set for PostgreSQL; never edit migration 1.
5. Run the full test suite against both backends before deploying.

## Security notes

- Secrets come only from environment variables / `.env`; `.env` is gitignored.
- The token is never logged. `Settings.bot_token` is excluded from its repr.
- Only `OWNER_IDS` seeded into `admins` (plus explicitly inserted admins) pass the
  admin guard.
- Do not expose this bot's database or logs publicly.
- `copy_message`/`copy_messages` are used; no forwarding attribution is added.

## Known limitations

- Live Telegram delivery was not tested because no bot token is available in this
  environment. Unit tests exercise the queue and all recovery logic with a fake
  sender.
- Telegram has no API to list pending join requests or fetch pre-existing channel
  history; both are documented limitations, not implementation shortcuts.
- SQLite is a single-writer database. This design is for one bot process. Running
  multiple polling instances against the same bot token will conflict.
- The default pacing is deliberately slow (3 seconds between copies). Adjust
  `SEND_DELAY_SECONDS` only after understanding Telegram's per-chat limits.
- Fuzzy or heavily obfuscated links can evade regex detection; the detector
  deliberately avoids over-blocking ordinary text.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `Configuration error: BOT_TOKEN is required` | `.env` exists next to the project and contains the real token. |
| Bot starts but no source messages indexed | Was the message posted **after** `/addsource`? Is the bot an admin? |
| `/adddest` says not an administrator | Add the bot as a chat administrator with Post Messages. |
| Messages are not copied | Check `/status` cursor/pause, bot posting permissions, and `/history` failures. |
| `Conflict: terminated by other getUpdates` | Stop the other bot process; only one polling worker per token. |
| Link deletion always fails | Give the bot Delete Messages and re-run `/linkprotection on`. |
| Join approvals fail | Give the bot Invite Users permission; use `/approvepending` for logged retries. |
| Scheduler time looks wrong | Verify `TIMEZONE=Asia/Kolkata`; timestamps in logs are local to the host, reports use IST. |

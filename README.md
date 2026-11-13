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
tests/                       79 automated tests + 26 optional PostgreSQL integration tests
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

`python -m app` is the local/SQLite runtime. It is a long-running worker: it uses
long polling, starts APScheduler, and must stay alive.

For PostgreSQL production, the runtime is split across two processes:

| Component | Entrypoint | Role |
| --- | --- | --- |
| Vercel serverless webhook | `api/telegram.py` | Validates updates, handles commands, enqueues work. Never publishes inline. |
| GitHub Actions worker | `python -m app.worker` | One bounded publishing pass per invocation. |

Never deploy `app/main.py` (polling plus APScheduler) to a serverless platform.
The deployment runbooks live in `deployment/`:

- `deployment/PHASE3_READINESS.md` — readiness, risks, cost, end-to-end test plan
- `deployment/SUPABASE_SETUP.md` — migrations, pooler DSN, roles, backups
- `deployment/VERCEL_SETUP.md` — webhook project and environment variables
- `deployment/GITHUB_ACTIONS_SETUP.md` — worker activation and limits
- `deployment/TELEGRAM_WEBHOOK_SETUP.md` — registration and verification
- `deployment/ROLLBACK.md` — emergency shutdown and rollback

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

## Database backends

SQLite is the default and remains the local-development and rollback backend.
PostgreSQL is opt-in:

```dotenv
DATABASE_BACKEND=postgres
DATABASE_URL=postgresql://user:password@host:6543/postgres?sslmode=require
DB_POOL_MIN=0
DB_POOL_MAX=2
DB_STATEMENT_CACHE_SIZE=0
```

`DB_STATEMENT_CACHE_SIZE=0` and a small pool are required behind Supabase's
transaction pooler (port 6543). The connection target is logged without
credentials.

### Schema and connection budget

Phase 1 runs against the connection's default `public` schema. A custom
`search_path` cannot be pinned reliably through a transaction-mode pooler
(session state is not guaranteed to stay on one backend connection), so the
application does not expose a `DB_SCHEMA` setting. Custom-schema migration is
possible only with a direct/session connection (`python -m app.db.migrate
--schema ...`), which is how the tests isolate themselves; it is not a supported
runtime configuration yet.

Pool size is a per-process budget:

| Runtime | Processes | Max clients | Notes |
|---|---|---|---|
| Vercel webhook (Hobby) | one per warm serverless instance | `DB_POOL_MAX` each | total = concurrent instances × `DB_POOL_MAX`; `DB_POOL_MIN=0` avoids idle clients on cold starts |
| GitHub Actions worker | exactly one short-lived process | `DB_POOL_MAX` | one bounded pass; `DB_POOL_MIN=0` because the process exits |

With the default `DB_POOL_MAX=2`, ten concurrently warm Vercel instances use at
most 20 pooler clients, and the Actions worker adds at most 2. Check the actual
client limit of the Supabase plan before raising these values; the default was
lowered from 5 because the workloads are short queries, not bulk work.

Transactions are bound to the asyncio task that opened them. A task created
inside a transaction captures that context even if it is awaited later, after
the parent transaction has closed. Using the database from such a child task
raises a clear `RuntimeError` instead of reusing the parent connection or
deadlocking a small pool. Create database-using child tasks outside the
transaction, or give them an explicitly clean context (for example
`asyncio.create_task(coro, context=contextvars.Context())`). A clean context
only prevents inheritance; the child still needs a free pool slot, so with a
small pool it must not be awaited to completion while the parent transaction
holds its connection.

PostgreSQL never migrates implicitly. Apply schema changes deliberately, then
start the bot:

```powershell
python -m app.db.migrate --dry-run     # inspect pending migrations
python -m app.db.migrate               # apply
```

The runner takes a transaction-level advisory lock (`pg_advisory_xact_lock`),
records a SHA-256 checksum per applied migration, and refuses to run when an
applied file changed or an applied version has no file. `--dry-run` writes
nothing.

`DATABASE_BACKEND=sqlite` (the default) keeps the existing auto-migration on
connect, so local development and rollback need no extra steps.

SQLite and PostgreSQL execute the same repository SQL. `app/db/sql_dialect.py`
rewrites the small set of SQLite-only constructs (`?` placeholders,
`INSERT OR IGNORE`, scalar `MAX`/`MIN`, implicit `rowid`) using a shared
SQL-aware scanner, so string literals, quoted identifiers and comments are never
modified. `app/db/migrations_pg/001_baseline.sql` mirrors the SQLite baseline and
`002_phase2_queue.sql` adds the PostgreSQL-only queue/worker schema. Boolean
columns stay `SMALLINT` 0/1 and timestamps stay ISO-8601 text for the Phase 1
tables so behaviour is identical on both backends. Phase 1 adds
`publishing_runs.insert_seq`, which replaces SQLite's implicit `rowid` for run
ordering. Only a `rowid` qualified by `publishing_runs` (or one of its aliases)
is rewritten, plus an unqualified `rowid` when `publishing_runs` is the
statement's only table; another table's qualified `rowid` is left untouched.

### Phase 2 PostgreSQL queue worker

On PostgreSQL, `/publishnow`, `/resume` and the daily scheduler enqueue durable
requests instead of publishing inline. `publish_requests`, `publishing_jobs`
and `publish_request_jobs` coalesce manual/resume work onto an active cycle and
deduplicate scheduled/catch-up cycles by date and slot. A separate bounded
worker executes one budgeted pass per invocation:

```powershell
DATABASE_BACKEND=postgres DATABASE_URL=... python -m app.worker
```

The worker claims jobs atomically with a 180-second lease and 30-second
heartbeat, confirms the shared global pacing permit immediately before each
send, and recovers expired leases with token fencing. Ambiguous network sends
are recorded as `uncertain` and never blindly resent. Limits per invocation are
950 total send attempts, a 50-minute work soft deadline, a 52-minute wall soft
deadline and a 58-minute hard deadline (assuming a 60-minute Actions timeout).
Billing charges each run with per-run ceiling rounding and reserves a full
3,600 seconds at admission against a 114,000-second internal monthly cap.

The pacing permit spaces send starts by at least 3.05 seconds. It cannot be
atomic with the network write: a process suspended after the final ownership and
local-deadline check can still overlap a replacement worker after its permit
expires. That residual post-check window is documented in
`app/services/pacing.py`. SQLite keeps the Phase 1 inline engine unchanged, and
the Phase 2 worker refuses to run on SQLite except in a dedicated backend-guard
test.

The parity test compares table/column structure, defaults, named index
definitions (ordered columns plus ASC/DESC), foreign keys (source column, target
table/column, `ON DELETE`), primary keys, unique constraints, and canonical
`CHECK` signatures. Collation, expression/partial indexes, and constraint
deferrability still require manual review.

Run the PostgreSQL integration tests against any disposable database; each test
uses and drops its own schema:

```powershell
$env:TEST_DATABASE_URL = "postgresql://postgres@127.0.0.1:5432/postgres"
pytest tests/test_pg_adapter.py -q
```

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

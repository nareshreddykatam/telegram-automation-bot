# Vercel Setup

Vercel hosts only the Telegram webhook and lightweight command handling. It does
not publish content and it does not run the scheduler.

## 1. What is deployed

```text
api/telegram.py        ASGI entrypoint for /api/telegram
app/webhook.py         Secret-header validation, update validation, fast ACK
app/bootstrap.py       Shared handler/service wiring (same as polling)
vercel.json            Function duration limit (60 seconds)
requirements.txt       Runtime dependencies installed by Vercel
```

The webhook runtime refuses `DATABASE_BACKEND=sqlite`. PostgreSQL is mandatory.
It never starts APScheduler and never calls `Dispatcher.start_polling`.

## 2. Import the project

1. In Vercel, **Add New → Project** and import the GitHub repository.
2. Branch: `migration/phase-1-postgres-adapter` for the prepared deployment, or
   the reviewed production branch after merge.
3. Framework preset: **Other**.
4. Root directory: repository root.
5. Build command: leave empty.
6. Output directory: leave empty.
7. Install command: leave default (`pip install -r requirements.txt`).
8. Python version: **3.12** (the project supports 3.11+; Vercel's current Python
   runtime should be pinned explicitly in project settings).
9. Do not add a `public/` directory or a static build.

`vercel.json` caps `api/telegram.py` at 60 seconds. The webhook is designed to
return in well under that, but the cap prevents a stuck handler from hanging the
platform default. Do not raise it without reviewing Vercel plan limits.

## 3. Environment variables

Set these in **Project → Settings → Environment Variables** for Production (and
Preview only if you intentionally use a separate test database).

| Variable | Sensitive? | Required value |
| --- | --- | --- |
| `BOT_TOKEN` | **Yes** | BotFather token. Can instead expose a `TELEGRAM_BOT_TOKEN` secret under this name. |
| `TELEGRAM_WEBHOOK_SECRET` | **Yes** | Random 1–256 chars: `A-Z a-z 0-9 _ -`. |
| `DATABASE_URL` | **Yes** | Supabase **transaction pooler** DSN, port 6543. |
| `OWNER_IDS` | No | Comma-separated numeric Telegram owner IDs. |
| `DATABASE_BACKEND` | No | `postgres` |
| `DB_SSL` | No | `require` (the DSN usually carries `sslmode=require`). |
| `DB_POOL_MIN` | No | `0` |
| `DB_POOL_MAX` | No | `1` for Vercel |
| `DB_STATEMENT_CACHE_SIZE` | No | `0` |
| `DB_COMMAND_TIMEOUT_SECONDS` | No | `30` |
| `DB_CONNECT_TIMEOUT_SECONDS` | No | `10` |
| `LOG_LEVEL` | No | `INFO` |
| `LOG_FILE` | No | Empty. Vercel's project filesystem is read-only. |
| `SCHEDULER_ENABLED` | No | `false` |
| `CATCHUP_ON_STARTUP` | No | `false` |
| `TIMEZONE` | No | `Asia/Kolkata` |
| `PUBLISH_HOUR` | No | `20` |
| `PUBLISH_MINUTE` | No | `0` |
| `MAX_CATCHUP_CYCLES` | No | `7` |
| `ADMIN_REPORTS` | No | `true` |
| `SEND_DELAY_SECONDS` | No | `3.0` |
| `CHUNK_DELAY_SECONDS` | No | `2.0` |
| `MAX_SEND_RETRIES` | No | `3` |

Do not add `DATABASE_PATH` as a production dependency. It is only used by the
SQLite rollback runtime. Do not set `DB_SCHEMA`.

## 4. Deploy

1. Apply Supabase migrations 001 and 002 first. See `SUPABASE_SETUP.md`.
2. Confirm the runtime DSN uses port **6543** and `sslmode=require`.
3. Deploy the Vercel project.
4. Open the deployment URL and check the health endpoint:

```text
GET https://PROJECT.vercel.app/api/telegram
```

Expected response:

```json
{"status":"ok","mode":"webhook"}
```

The health endpoint does not connect to the database, so it is a liveness check,
not a full readiness check. A full readiness check happens on the first real
update.

5. Register the webhook only after the health check and database migration pass.
   Follow `TELEGRAM_WEBHOOK_SETUP.md`.

## 5. Security behavior

Implemented in `app/webhook.py`:

- POST only. Other methods return `405`.
- `X-Telegram-Bot-Api-Secret-Token` is compared in constant time. Missing or
  wrong values return `403`.
- `Content-Type` must be `application/json`; otherwise `415`.
- Bodies larger than 1 MiB are rejected with `413`.
- Malformed JSON or an invalid Telegram update returns `400`.
- Handler failures return `500` so Telegram can retry. Database uniqueness
  constraints prevent duplicate source messages, requests and jobs.
- The bot token, database URL and webhook secret are never logged.
- The runtime refuses SQLite, so an ephemeral Vercel filesystem cannot silently
  become the production database.

## 6. Serverless behavior and limits

- The first request after a cold start builds the asyncpg pool and aiogram
  dispatcher. Expect roughly 1–3 seconds.
- The database pool is per warm instance. Keep `DB_POOL_MAX=1` unless you have
  measured a need and checked the Supabase pooler limit.
- Do not run `/publishnow`, `/resume`, or scheduled publishing inline. On
  PostgreSQL those commands only create durable queue rows. Actual sends happen
  in GitHub Actions.
- `/resume` without a destination spawns a short PostgreSQL catch-up enqueue.
  The webhook waits up to 10 seconds for spawned background tasks before
  returning.
- Vercel Hobby is intended for personal, non-commercial projects. If this bot is
  used commercially, Vercel's terms require an appropriate paid plan.
- The function must not hold a Telegram send or a long sleep. The 60-second
  duration cap is a backstop, not a budget.

## 7. Troubleshooting

| Symptom | Check |
| --- | --- |
| `Configuration error: TELEGRAM_WEBHOOK_SECRET is required` | Set the secret in Vercel and redeploy. |
| `The webhook runtime requires DATABASE_BACKEND=postgres` | Set `DATABASE_BACKEND=postgres`. |
| 403 from Telegram retries | The secret passed to `setWebhook` does not match Vercel. |
| 500 with `update_processing_failed` | Check Vercel logs; verify Supabase status and pooler DSN. |
| Cold-start timeouts | Confirm `DB_POOL_MIN=0`, `DB_POOL_MAX=1`, and that the database is not paused. |
| No commands work | Check `getWebhookInfo`, Vercel function logs, and BotFather token. |
| Publishing never starts | This is expected. The Vercel function only enqueues; activate the GitHub Actions worker. |

## 8. Rollback

Use Vercel's **Deployments → Promote** to return to the previous known-good
deployment, or remove the webhook and return to the local polling runtime. The
full emergency procedure is in `ROLLBACK.md`.

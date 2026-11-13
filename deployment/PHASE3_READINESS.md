# Phase 3 Deployment Readiness

Deployment preparation for the Phase 1 (PostgreSQL adapter) and Phase 2
(durable publishing queue) work. This document is the sign-off artifact for the
target stack:

```text
Telegram  ->  Vercel Hobby (webhook)  ->  Supabase Free (PostgreSQL)
                                              ^
GitHub Actions Free (daily worker) ----------/
```

**Phase 3 status: prepared, not deployed.** No remote service was accessed,
created, or modified. No webhook was registered. No Telegram message was sent.
No secret was created. Nothing was committed or pushed.

---

## 1. Readiness summary

| Area | State | Evidence |
| --- | --- | --- |
| Configuration and secrets | Ready | `app/config.py`, `.env.production.example`; secret redaction and alias tested |
| PostgreSQL backend | Ready | `app/db/`, migrations 001 + 002 verified on a disposable cluster |
| Transaction-pooler compatibility | Ready in code, unverified against Supabase | `DB_STATEMENT_CACHE_SIZE=0`; no session state, `LISTEN`, or session advisory locks in the request path |
| Webhook application | Ready | `app/webhook.py`, `api/telegram.py`, `vercel.json`; 10 tests |
| Webhook security | Ready | Constant-time secret header, POST-only, JSON-only, 1 MiB cap, 500-on-failure |
| Worker | Ready | `app/worker.py`, `app/services/worker.py`, `app/services/billing.py` |
| Worker schedule template | Ready, inactive | `deployment/worker.yml.template`; no file under `.github/workflows/` |
| Migrations | Ready | `app/db/migrate.py`; checksum-verified, advisory-locked, dry-run capable |
| Logging / secret hygiene | Ready | Console fallback for read-only filesystems; DSN described without credentials |
| Rollback | Documented | `deployment/ROLLBACK.md`, SQLite path retained |
| Live end-to-end test | **Pending activation window** | Requires a test destination and real Telegram calls |

No code blocker prevents deployment. The remaining gates are operational:
verify the action SHAs, run the live end-to-end test, and supply secrets.

---

## 2. Production topology

```text
                          ┌──────────────────────────────────────┐
  Telegram Bot API ──────►│ Vercel /api/telegram                 │
  (HTTPS POST, secret)    │  - ASGI, no polling, no scheduler    │
                          │  - validates update, handles command │
                          │  - writes durable PostgreSQL rows    │
                          └──────────────┬───────────────────────┘
                                         │ transaction pooler :6543
                                         ▼
                          ┌──────────────────────────────────────┐
                          │ Supabase PostgreSQL                  │
                          │  publish_requests, publishing_jobs,  │
                          │  worker_runs, telegram_rate_limit    │
                          └──────────────▲───────────────────────┘
                                         │ transaction pooler :6543
                          ┌──────────────┴───────────────────────┐
  GitHub Actions 20:00 IST│ python -m app.worker                 │
  (one bounded pass)      │  - admits against monthly cap        │
                          │  - claims leases, sends copies       │
                          └──────────────────────────────────────┘
```

The webhook never publishes. The worker never accepts updates. A single
PostgreSQL database is the only shared state, so either side can restart without
losing queued work.

---

## 3. What changed in this phase

Application code (only where deployment required it):

| File | Change | Reason |
| --- | --- | --- |
| `app/bootstrap.py` | New shared `BotApplication` / `build_application` | Webhook and polling must share identical wiring |
| `app/main.py` | Uses bootstrap; remains the polling/SQLite runtime | Rollback path unchanged |
| `app/webhook.py` | New dependency-free ASGI webhook | Secret check, validation, fast ACK, no inline publishing |
| `api/telegram.py` | New Vercel entrypoint exporting `app` | Serverless routing to `/api/telegram` |
| `vercel.json` | `maxDuration: 60` for the webhook | Backstop against a stuck handler |
| `app/config.py` | `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN` alias | Webhook auth; hosting-dashboard naming |
| `app/logging_config.py` | Console fallback when the log file is unwritable | Vercel project filesystem is read-only |
| `app/services/background.py` | `wait(timeout)` | Flush spawned tasks before the function freezes |
| `.gitignore` | Unignore `.env.production.example` | Publish a placeholder template, never a real env file |

Deployment artifacts (docs and templates only):

- `deployment/PHASE3_READINESS.md` (this file)
- `deployment/SUPABASE_SETUP.md`
- `deployment/VERCEL_SETUP.md`
- `deployment/GITHUB_ACTIONS_SETUP.md`
- `deployment/TELEGRAM_WEBHOOK_SETUP.md`
- `deployment/ROLLBACK.md`
- `deployment/worker.yml.template`
- `.env.production.example`

Tests added: `tests/test_phase3_webhook.py`, `tests/test_logging.py`,
`tests/test_deployment_artifacts.py`, plus webhook/alias cases in
`tests/test_config.py`.

---

## 4. Environment inventory

### 4.1 Secrets (never commit, never log)

| Name | Used by | Notes |
| --- | --- | --- |
| `BOT_TOKEN` | Vercel, worker | BotFather token; canonical name |
| `TELEGRAM_BOT_TOKEN` | GitHub secret name only | Mapped to runtime `BOT_TOKEN` by the workflow |
| `DATABASE_URL` | Vercel, worker | Supabase **transaction pooler** DSN, port 6543, `sslmode=require` |
| `TELEGRAM_WEBHOOK_SECRET` | Vercel | 1–256 chars of `A-Z a-z 0-9 _ -`; generated with `secrets.token_urlsafe` |

### 4.2 Non-sensitive configuration

| Name | Production value | Why |
| --- | --- | --- |
| `DATABASE_BACKEND` | `postgres` | Webhook refuses SQLite |
| `DB_SSL` | `require` | Encrypt in transit |
| `DB_POOL_MIN` | `0` | No idle pooler clients on cold starts |
| `DB_POOL_MAX` | `1` (Vercel) / `2` (worker) | Per-process budget |
| `DB_STATEMENT_CACHE_SIZE` | `0` | Mandatory behind a transaction pooler |
| `DB_COMMAND_TIMEOUT_SECONDS` | `30` | Bound slow queries |
| `DB_CONNECT_TIMEOUT_SECONDS` | `10` | Fail fast on a paused project |
| `OWNER_IDS` | numeric IDs | Seeds the `admins` table |
| `TIMEZONE` | `Asia/Kolkata` | Reporting/schedule dates |
| `PUBLISH_HOUR` / `PUBLISH_MINUTE` | `20` / `0` | Daily publish time |
| `SCHEDULER_ENABLED` | `false` | No APScheduler in production |
| `CATCHUP_ON_STARTUP` | `false` | Catch-up is explicit via queue |
| `ADMIN_REPORTS` | `true` | Owner notifications |
| `LOG_LEVEL` | `INFO` | Platform log viewer |
| `LOG_FILE` | empty | Read-only serverless filesystem |
| `MAX_CATCHUP_CYCLES` | `7` | Bounded missed-day processing |
| `SEND_DELAY_SECONDS` | `3.0` | Telegram pacing |
| `CHUNK_DELAY_SECONDS` | `2.0` | Telegram pacing |
| `MAX_SEND_RETRIES` | `3` | Transient failure handling |

Full placeholder file: `.env.production.example`.

### 4.3 Not used in production

`DATABASE_PATH`, `DB_SCHEMA`, `db_pool_min=1`. SQLite is the local/rollback
backend only; PostgreSQL data is not automatically exported to SQLite.

---

## 5. Activation runbook (ordered)

Nothing here has been executed. Each step has a dedicated runbook.

1. **Verify action pins** — `deployment/GITHUB_ACTIONS_SETUP.md` §5.
   Re-run `git ls-remote` for `v4.2.2` / `v5.6.0` and update the template if a
   SHA moved.
2. **Create Supabase project** — `deployment/SUPABASE_SETUP.md` §1.
3. **Take a pre-migration backup** — `SUPABASE_SETUP.md` §7 (Free has no PITR).
4. **Apply migrations 001 + 002 over the direct DSN** — `SUPABASE_SETUP.md` §3.
   Dry-run first; expect `0 applied, 2 pending`, then `Applied migrations: 1, 2`.
5. **Create the least-privilege runtime role** — `SUPABASE_SETUP.md` §4.
6. **Create the Vercel project and set environment variables** —
   `deployment/VERCEL_SETUP.md` §2–§3.
7. **Deploy and check `GET /api/telegram`** — expect
   `{"status":"ok","mode":"webhook"}`.
8. **Confirm the unauthenticated POST is rejected with 403** —
   `deployment/TELEGRAM_WEBHOOK_SETUP.md` §2.
9. **Register the webhook with `secret_token`** — `TELEGRAM_WEBHOOK_SETUP.md` §3.
10. **Verify with `getWebhookInfo`** — no persistent `last_error_message`.
11. **Create GitHub secrets/variables and activate the workflow** —
    `GITHUB_ACTIONS_SETUP.md` §3, §6.
12. **Run one manual confirming dispatch** and inspect the log for
    `admitted=True`, expected `stopped=`, and no secret leakage.
13. **Run the end-to-end test plan** in section 7.
14. **Only then** leave the daily schedule enabled.

---

## 6. Worker and webhook operating limits

| Control | Value | Source |
| --- | --- | --- |
| Webhook function cap | 60 s | `vercel.json` |
| Webhook background flush | 10 s | `app/webhook.py` |
| Webhook max body | 1 MiB | `app/webhook.py` |
| Worker job timeout | 60 min | workflow template |
| Worker hard deadline | 58 min | `app/services/worker.py` |
| Worker soft deadlines | 50 min work / 52 min wall | `app/services/worker.py` |
| Send attempts per run | 950 | `app/services/worker.py` |
| Monthly internal cap | 1,900 min (114,000 s) | `app/services/billing.py` |
| Platform monthly allowance | 2,000 min (120,000 s) | GitHub Free private |
| Abandoned-run reaper | 4,200 s (70 min) | `app/services/billing.py` |
| Lease / heartbeat | 180 s / 30 s | `app/services/worker.py` |

The 1,900-minute internal cap is deliberately below the 2,000-minute GitHub
allowance so manual runs cannot exhaust the account.

---

## 7. End-to-end test plan

No real message is sent during preparation. The plan runs in three stages; only
stage C touches Telegram, and only during an authorized window.

### Stage A — local, no network (already passing)

- Webhook unit tests: secret, method, content type, body size, malformed update,
  dispatcher failure, health, lifespan, background flush.
- Config tests: secret regex, 256-char limit, `TELEGRAM_BOT_TOKEN` alias.
- Logging test: unwritable log path falls back to console.
- Deployment artifact tests: `vercel.json`, env template, workflow template,
  required docs, template not activated.

### Stage B — local disposable PostgreSQL, fake sender

1. Start the disposable cluster on `127.0.0.1:55432`.
2. Create a fresh database and apply migrations 001 + 002.
3. Drive the queue directly with the test suite (`tests/test_phase2_pg.py`),
   which uses a fake sender. No Telegram call is made.
4. Assert one lease, one job, one `worker_runs` row per admitted pass; assert the
   950-attempt and 1,900-minute caps stop the run; assert expired-lease recovery
   and accounting reaping.

### Stage C — authorized live test (not run now)

Preconditions:

- a **separate Telegram test destination** (test channel or test group) that is
  not a real audience,
- a **test source** with 2–3 non-sensitive messages,
- the production Vercel deployment and worker pointed at a **staging**
  Supabase project (or a separate database),
- the real owners warned that test copies will appear.

Steps:

1. Add the test source and test destination (`/addsource`, `/adddest`) and set a
   small batch size (for example 2).
2. Queue one request with `/publishnow` (or the scheduled path). Confirm the
   webhook returns quickly and only durable rows are written.
3. Trigger the worker with `workflow_dispatch confirm=run`.
4. Confirm exactly one copy of each message appears in the test destination.
5. Confirm `/history` reports one completed run and no `sending` rows remain.
6. Trigger a second dispatch immediately: it must find no work and stop `idle`.
7. Restart the webhook (redeploy) and re-deliver the same update id: no duplicate
   source row, request, job, or copy.
8. Simulate a transient failure (invalid destination or revoked permission):
   the worker must return the job to `retry`, not `completed`.
9. Simulate a crashed worker (cancel the job mid-run). After the lease expires,
   the next run must recover the lease and avoid a duplicate copy; the accounting
   reaper must charge the abandoned run at 60 minutes.
10. Verify no token, DSN, password, or secret header appears in Vercel, Actions,
    or Supabase logs.
11. Delete the test webhook, disable the worker, and remove the test rows through
    the documented procedure.

Acceptance: one copy per message, no duplicates after retry or restart, caps
respected, zero secret leakage.

---

## 8. Risk register

| # | Risk | Impact | Mitigation | Residual |
| --- | --- | --- | --- | --- |
| R1 | Supabase transaction-pooler behavior is not remotely verified | Runtime SQL failure | `DB_STATEMENT_CACHE_SIZE=0`, no session state, no `LISTEN`, no session advisory locks in request paths; migrations use a direct DSN | Medium until Stage C |
| R2 | Action SHAs were not verified (no remote access allowed) | Supply-chain risk | Re-run `git ls-remote` before activation; keep tag comments | Low, gated |
| R3 | Vercel Hobby is for personal, non-commercial use | Account action if commercial | Accept only for personal use; upgrade to Pro if commercial | Medium, policy |
| R4 | Supabase Free pauses after 7 days without activity | Bot stops responding | Daily worker keeps it active; documented restore steps | Low |
| R5 | Supabase Free has no PITR | Data loss on operator error | Manual `pg_dump` before migration and after accepted changes; restore rehearsal | Medium |
| R6 | Private-repo Actions minutes: ~1,800 of 2,000 used | Failed runs near month end | 1,900-minute internal cap; no push-triggered runs; keep manual dispatches few | Low |
| R7 | Telegram retries a 5xx after partial side effects | Duplicate admin reply | Source/request/job uniqueness constraints; publishing is queue-only | Low |
| R8 | Cold start + Supabase wake-up exceeds webhook budget | Telegram retry/timeout | `DB_POOL_MIN=0`, 60 s cap, retries are idempotent | Low |
| R9 | Misconfigured env makes `api/telegram.py` fail at import | All requests 500 | Health check fails visibly; Vercel logs the config error (no secret values) | Low |
| R10 | Vercel Python ASGI support must be confirmed on first real deploy | Deployment friction | Entrypoint follows the documented `app` ASGI convention; verify at step 7 | Low |
| R11 | SQLite rollback loses PostgreSQL history | Data gap | Explicit warning in `ROLLBACK.md`; export only after an authorized procedure | Accepted |
| R12 | A cancelled worker holds a lease and a run row | Blocked destination / accounting | 180 s lease + heartbeat; expired-lease recovery; 70 min reaper | Low |

---

## 9. Monthly cost estimate (INR)

Assumption: 1 USD ≈ ₹88 (indicative; verify at activation). Target: ₹0/month.

| Service | Plan | Included | Monthly cost |
| --- | --- | --- | --- |
| Vercel | Hobby | 100 GB bandwidth, serverless functions, non-commercial | ₹0 |
| Supabase | Free | 500 MB database, 2 GB egress, 50k MAU; pauses after 7 idle days | ₹0 |
| GitHub Actions | Free, private repo | 2,000 Linux minutes/month (public repos are effectively unlimited) | ₹0 |
| Telegram Bot API | Free | Bot API | ₹0 |
| Domain | None | `*.vercel.app` | ₹0 |
| **Total** | | | **₹0** |

Optional paid upgrades (only if needed):

| Upgrade | Approx. USD/month | Approx. INR/month | When |
| --- | --- | --- | --- |
| Vercel Pro | $20 | ₹1,760 | Commercial use or team features |
| Supabase Pro | $25 | ₹2,200 | PITR, no pause, more storage |
| Extra Actions minutes | $0.008/min | ₹0.70/min | Only if >2,000 min/month |
| Custom domain | ~$10–15/year | ~₹75–110/month equivalent | Branding |

Cost traps: repeated manual worker dispatches consume minutes; a push-triggered
workflow would multiply usage; a public repository makes Actions effectively
free but exposes the code (never secrets).

---

## 10. Rollback summary

Full procedure: `deployment/ROLLBACK.md`.

1. `/pause` from Telegram if the bot responds.
2. Disable the Actions workflow.
3. Cancel a running worker job.
4. `deleteWebhook` so Vercel receives nothing.
5. Promote the previous Vercel deployment.
6. Preserve logs and take a final `pg_dump`.
7. Restore the last good dump or return to the previous deployment.
8. Run the migration dry-run (`0 pending`) before restarting.
9. SQLite/local mode is available but **does not inherit PostgreSQL data**.

---

## 11. Verification results

Run locally on the preparation machine. PostgreSQL tests use a disposable
cluster on `127.0.0.1:55432`; no remote database is contacted.

| Check | Command | Result |
| --- | --- | --- |
| Phase 3 targeted tests | `pytest tests/test_phase3_webhook.py tests/test_logging.py tests/test_config.py tests/test_deployment_artifacts.py` | **25 passed** |
| Full SQLite/default suite | `pytest` (no `TEST_DATABASE_URL`) | **124 passed, 70 skipped** |
| Full PostgreSQL suite | `TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres pytest` | **194 passed** |
| Lint | `ruff check app api tests` | **All checks passed** |
| Byte-compile | `python -m compileall -q app api` | **OK** |
| Migrations on fresh DB | dry-run → apply → dry-run | **0 applied / 2 pending → applied 1, 2 → up to date** |

Environment: Windows, Python from `.venv`, PostgreSQL 18.6 on
`127.0.0.1:55432`, disposable database deleted after the run. No remote service
was contacted.

---

## 12. Compliance confirmation

- No Vercel project was created or modified.
- No Supabase project or database was created or modified.
- No GitHub workflow was activated, no secret or variable was created.
- No Telegram webhook was registered, deleted, or called.
- No real Telegram message was sent.
- No commit or push was made.
- No Phase 4 work was started.
- All external unknowns (pooler behavior, action SHAs, live delivery) are
  explicitly gated behind the activation window.

---

## 13. Sign-off checklist

- [ ] Action SHAs re-verified against official tags.
- [ ] Supabase project created; pre-migration backup stored off-repository.
- [ ] Migrations 001 + 002 applied; dry-run reports `0 pending`.
- [ ] Runtime role has DML only (DDL fails).
- [ ] Vercel env vars set; `GET /api/telegram` returns the health JSON.
- [ ] Unauthenticated POST returns 403.
- [ ] Webhook registered with `secret_token`; `getWebhookInfo` clean.
- [ ] GitHub secrets/variables set; workflow activated.
- [ ] One manual dispatch succeeds with `admitted=True`.
- [ ] Stage C end-to-end test passes with exactly one copy per message.
- [ ] Logs contain no secret material.
- [ ] Rollback runbook rehearsed at least once.

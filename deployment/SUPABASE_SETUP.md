# Supabase Setup

Prepare the database only. Do **not** run these commands against a remote
Supabase project during Phase 3 preparation. They are the activation runbook.

## 1. Project and plan

1. Create a Supabase project in the region closest to the GitHub Actions runner
   and the Vercel region. For this project, `ap-south-1` (Mumbai) is usually the
   best latency choice if available.
2. Use the Free plan initially. The Free plan is suitable for this workload, but
   it has no point-in-time recovery and pauses after 7 days without activity.
   The daily worker normally keeps it active.
3. Save the database password in a password manager. Never put it in Git, a
   document, an Actions log, or a Vercel build log.

## 2. Connection strings

Supabase exposes several connection paths. They serve different purposes:

| Purpose | Path | Port | Notes |
| --- | --- | --- | --- |
| Migrations / DDL | Direct connection | 5432 | Preferred. Full session, DDL, advisory locks. |
| Migrations alternative | Session pooler | 5432 | Use only if direct IPv6 is unavailable. |
| Runtime (Vercel + worker) | Transaction pooler | 6543 | Required mode for serverless; prepared statements disabled. |

Runtime DSN shape:

```text
postgresql://RUNTIME_ROLE.PROJECT_REF:PASSWORD@REGION.pooler.supabase.com:6543/postgres?sslmode=require
```

Migration DSN shape:

```text
postgresql://postgres:REPLACE_PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres?sslmode=require
```

`sslmode=require` encrypts the connection. Prefer `verify-full` if you have
configured Supabase's CA certificate and the client supports it. Do not disable
SSL for production.

## 3. Apply migrations 001 and 002

Run these from a trusted machine with the repository checked out. Use the
**direct/session** DSN, not the transaction pooler.

```powershell
$env:MIGRATION_DATABASE_URL = "postgresql://postgres:REPLACE_PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres?sslmode=require"

# 1. Inspect first. Nothing is written.
.venv\Scripts\python.exe -m app.db.migrate `
  --dsn $env:MIGRATION_DATABASE_URL --ssl require --dry-run

# Expected on a new project:
#   INFO ... 0 applied, 2 pending (dry run, nothing written)
#   Pending migrations: 1, 2

# 2. Apply 001_baseline.sql and 002_phase2_queue.sql atomically.
.venv\Scripts\python.exe -m app.db.migrate `
  --dsn $env:MIGRATION_DATABASE_URL --ssl require

# Expected:
#   INFO Applied 001_baseline.sql
#   INFO Applied 002_phase2_queue.sql
#   Applied migrations: 1, 2

# 3. Confirm there is nothing pending.
.venv\Scripts\python.exe -m app.db.migrate `
  --dsn $env:MIGRATION_DATABASE_URL --ssl require --dry-run

# Expected: Database is up to date.
```

The runner takes `pg_advisory_xact_lock`, records a SHA-256 checksum for each
migration, and refuses to run if an applied file changed or an applied version
has no file. Never edit `001_baseline.sql` or `002_phase2_queue.sql`; add a new
numbered migration instead.

## 4. Create a least-privilege runtime role

The migration role needs DDL. The runtime role must not have DDL or ownership.
Run this once in the Supabase SQL editor as the project owner, after migrations:

```sql
-- Replace the password locally before running. Do not save the filled SQL.
CREATE ROLE bot_runtime WITH LOGIN PASSWORD 'REPLACE_WITH_A_LONG_RANDOM_PASSWORD'
  NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE;

GRANT CONNECT ON DATABASE postgres TO bot_runtime;
GRANT USAGE ON SCHEMA public TO bot_runtime;

-- Runtime DML only. No CREATE / ALTER / DROP.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bot_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bot_runtime;

-- Future objects created by the migration owner should also be usable.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bot_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO bot_runtime;
```

Notes:

- The application does not create or migrate schema. It only runs DML and
  transaction-scoped advisory locks.
- Supabase's pooler uses the `RUNTIME_ROLE.PROJECT_REF` username form for pooler
  connections. Use the exact username shown in the Supabase connection dialog;
  do not guess it.
- If a custom role is awkward with the pooler, the default `postgres` role also
  works, but it has far more privilege than the application needs.

## 5. Runtime configuration

Use the transaction-pooler DSN for both Vercel and the GitHub Actions worker:

```dotenv
DATABASE_BACKEND=postgres
DATABASE_URL=postgresql://bot_runtime.PROJECT_REF:REPLACE_PASSWORD@REGION.pooler.supabase.com:6543/postgres?sslmode=require
DB_SSL=require
DB_POOL_MIN=0
DB_POOL_MAX=1
DB_STATEMENT_CACHE_SIZE=0
```

Why these values:

- `DB_STATEMENT_CACHE_SIZE=0` is mandatory behind a transaction pooler.
  Prepared statements can be pinned to a different backend connection.
- `DB_POOL_MIN=0` avoids idle pooler clients on Vercel cold starts.
- Pool size is per process. With `DB_POOL_MAX=1`, ten warm Vercel instances use
  at most ten pooler clients. The worker may use `DB_POOL_MAX=2`.
- Keep `DB_COMMAND_TIMEOUT_SECONDS=30` and `DB_CONNECT_TIMEOUT_SECONDS=10`
  unless the pooler is consistently slow.
- Never set a custom `search_path` or `DB_SCHEMA`. Session state is not stable
  through a transaction pooler.

## 6. Verification

Run these read-only checks with the direct connection:

```sql
SELECT version, length(checksum) AS checksum_length
FROM schema_migrations
ORDER BY version;

-- Expected: versions 1 and 2, each checksum_length = 64.

SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
    'publish_requests', 'publishing_jobs', 'publish_request_jobs',
    'worker_runs', 'telegram_rate_limit'
  )
ORDER BY table_name;

-- Expected: 5 rows.
```

Then verify the runtime role can perform DML without DDL:

```sql
-- Run as bot_runtime after connecting with its pooler DSN.
SELECT current_user, current_database();
SELECT count(*) FROM publishing_jobs;

-- This must fail:
-- CREATE TABLE should_not_work (id int);
```

## 7. Backup before activation

Take a manual backup before the first migration and after any accepted data
change. Supabase Free has no PITR.

```powershell
$env:PGPASSWORD = "REPLACE_PASSWORD"
pg_dump --host db.PROJECT_REF.supabase.co --port 5432 --username postgres `
  --dbname postgres --format=custom --no-owner --no-privileges `
  --file "backups/supabase-before-phase3.dump"
Remove-Item Env:PGPASSWORD
```

Store the dump outside the repository and encrypt it. Test a restore into a
disposable local PostgreSQL database before relying on the backup.

```powershell
createdb -h 127.0.0.1 -p 5432 -U postgres restore_check
pg_restore --host 127.0.0.1 --port 5432 --username postgres `
  --dbname restore_check --no-owner --no-privileges `
  "backups/supabase-before-phase3.dump"
```

## 8. Free-tier pause and inactivity

- Free projects pause after 7 days without database activity.
- The daily GitHub Actions worker normally prevents this.
- A paused project rejects connections. Restore it from the Supabase dashboard,
  then run the dry-run migration check and one worker manual dispatch.
- If you disable the worker for more than 7 days, expect to restore the project.
- Free plan storage is 500 MB. This bot stores text metadata only (no media), so
  normal usage is far below that; monitor the dashboard anyway.
- There is no automatic PITR. A mistaken manual deletion requires the latest
  manual dump.

## 9. Database rollback

Preferred order:

1. Stop the worker workflow and remove the Telegram webhook.
2. Take a final `pg_dump` so the failed state is available for investigation.
3. Restore the last good dump into a new Supabase project or a new database.
4. Point `DATABASE_URL` at the restored database.
5. Run the migration dry-run. It must report 0 pending before restarting.
6. Re-register the webhook and re-enable the worker.

Do not repair a corrupt production database by editing or deleting rows in
`schema_migrations`. Restore the snapshot instead. A schema downgrade of Phase 2
tables is not required for application rollback; the extra tables are inert if
the application is not using PostgreSQL.

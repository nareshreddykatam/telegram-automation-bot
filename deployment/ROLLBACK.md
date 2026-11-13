# Emergency Shutdown and Rollback

Use this runbook if publishing is sending the wrong content, the worker loops,
the database is unhealthy, or the webhook is compromised. Do not experiment on
production while a worker holds a lease.

## 1. Stop new work, fastest first

1. **Pause publishing from Telegram** if the bot still responds:

```text
/pause
```

2. **Disable the GitHub Actions workflow**:
   **Actions → Publishing worker → ... → Disable workflow**.

3. **Cancel a running worker job** in the Actions UI. Cancellation does not undo
   an in-flight Telegram copy; pausing first reduces the chance of another send.

4. **Remove the Telegram webhook** so no new updates reach Vercel:

```powershell
$env:TELEGRAM_BOT_TOKEN = "REPLACE_WITH_BOT_TOKEN"
Invoke-RestMethod `
  -Method Post `
  -Uri "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/deleteWebhook" `
  -Body @{ drop_pending_updates = $false }
Remove-Item Env:TELEGRAM_BOT_TOKEN
```

5. **Roll back Vercel** to the previous known-good deployment:
   **Deployments → select previous → Promote to Production**. This stops broken
   webhook code from processing further updates.

6. **Preserve evidence**: download the current Vercel, Actions and Supabase logs
   before deleting or rotating anything.

## 2. Freeze database writes

With the worker disabled and webhook removed, no application process should be
writing. Confirm from Supabase:

```sql
SELECT count(*) FROM publishing_jobs WHERE status = 'running';
-- Expected: 0 after leases expire/recovery, or a small number from a cancelled run.

SELECT count(*) FROM worker_runs WHERE status = 'running';
-- Expected: 0 after the abandoned-run reaper or manual accounting.
```

Do not manually delete `publishing_jobs` or `publishing_history` to “fix” state.
Use the application's recovery paths or restore a backup.

## 3. Take a final backup

```powershell
$env:PGPASSWORD = "REPLACE_PASSWORD"
pg_dump --host db.PROJECT_REF.supabase.co --port 5432 --username postgres `
  --dbname postgres --format=custom --no-owner --no-privileges `
  --file "backups/incident-2026-XX-XX.dump"
Remove-Item Env:PGPASSWORD
```

Store it encrypted outside the repository.

## 4. Roll back the application

### Option A — return to the previous Vercel deployment

Best for a webhook-only regression. The database remains PostgreSQL and the
worker stays disabled until verified. Promote the previous deployment, re-register
the webhook with the matching secret, then re-enable the workflow.

### Option B — restore the database backup

1. Create a new Supabase project or a new database in the existing project.
2. Restore the last good dump:

```powershell
$env:PGPASSWORD = "REPLACE_PASSWORD"
pg_restore --host db.PROJECT_REF.supabase.co --port 5432 --username postgres `
  --dbname postgres --no-owner --no-privileges `
  "backups/last-good.dump"
Remove-Item Env:PGPASSWORD
```

3. Run the migration dry-run against the restored database:

```powershell
.venv\Scripts\python.exe -m app.db.migrate `
  --dsn $env:MIGRATION_DATABASE_URL --ssl require --dry-run
```

Expected: `0 pending` / `Database is up to date.` If it reports pending or a
checksum mismatch, stop and investigate before starting anything.

4. Point `DATABASE_URL` at the restored database.
5. Re-register the webhook and re-enable the worker only after the controlled test.

### Option C — SQLite/local operation

SQLite is a supported local and rollback backend, but **PostgreSQL data is not
automatically exported to SQLite**. Switching backends discards production
history unless you have a recent SQLite database and accept the data gap.

Use this only when:

- the PostgreSQL data is not needed, or
- you have a known-good SQLite file from before the PostgreSQL cutover, or
- an authorized export/migration procedure has already been completed.

Procedure:

1. Stop the webhook and worker as in section 1.
2. On a persistent local machine or VPS, check out the last known-good branch.
3. Set:

```dotenv
DATABASE_BACKEND=sqlite
DATABASE_PATH=/absolute/persistent/path/bot.db
LOG_FILE=/absolute/persistent/path/bot.log
SCHEDULER_ENABLED=true
```

4. Start the polling runtime:

```powershell
.venv\Scripts\python.exe -m app
```

5. Re-add sources/destinations if the SQLite file is empty. Cursors start where
   they were left in that SQLite file; they do not inherit Supabase progress.
6. Confirm only one process is polling the bot token.

Do not run SQLite polling and the PostgreSQL worker against the same Telegram
bot at the same time.

## 5. Verify the rollback

- `getWebhookInfo` has no persistent `last_error_message` (if webhook mode).
- The worker workflow is disabled (if database is frozen).
- `/status` shows expected destinations, cursors and pause state.
- `/history` shows no unexpected `sending` rows.
- A single controlled test destination receives exactly one copy.
- No duplicate copy appears after a restart.
- Vercel/Actions/Supabase logs contain no token or DSN.

## 6. Rotate credentials if exposure is suspected

1. Rotate the BotFather token (`/revoke`).
2. Rotate `TELEGRAM_WEBHOOK_SECRET`.
3. Rotate the Supabase runtime password and, if necessary, the migration role
   password.
4. Update Vercel and GitHub secrets.
5. Re-register the webhook with the new secret.
6. Redeploy/re-run and verify.

## 7. Post-incident

- Record the time of the first bad send, affected destinations, and message IDs.
- Keep the incident dump and logs for at least the retention period.
- Add a regression test if the cause was application logic.
- Do not re-enable the schedule until the controlled test passes.

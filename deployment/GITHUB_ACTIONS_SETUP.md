# GitHub Actions Worker Setup

The worker runs one bounded publishing pass per invocation. It is the only
component allowed to send scheduled/manual queued content in PostgreSQL mode.

## 1. Template location

The prepared template is **outside** the active workflow directory:

```text
deployment/worker.yml.template
```

There is intentionally no file in `.github/workflows/` yet. Do not activate the
workflow until the checklist in `PHASE3_READINESS.md` is complete.

## 2. Schedule and triggers

| Item | Value |
| --- | --- |
| Daily schedule | `30 14 * * *` UTC |
| Local time | 20:00 Asia/Kolkata (IST has no DST) |
| Manual trigger | `workflow_dispatch` with confirmation input `run` |
| Push trigger | None |
| Pull-request trigger | None |
| Concurrency | One `telegram-publishing-worker` group; no cancellation |
| Job timeout | 60 minutes |
| Worker internal hard deadline | 58 minutes |
| Send attempt limit | 950 |
| Internal monthly cap | 1,900 minutes (114,000 seconds) |

GitHub's scheduled cron can start a few minutes late under load. The worker
deadlines assume the **60-minute job timeout**, so keep them aligned.

## 3. Required repository configuration

Secrets (**Settings → Secrets and variables → Actions → Secrets**):

| Name | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather token; the workflow maps it to runtime `BOT_TOKEN`. |
| `DATABASE_URL` | Supabase transaction-pooler DSN, port 6543, `sslmode=require`. |

Variables (**Settings → Secrets and variables → Actions → Variables**):

| Name | Purpose |
| --- | --- |
| `OWNER_IDS` | Comma-separated numeric Telegram owner IDs. |

Do not create these during Phase 3 preparation. The template documents them for
activation day.

The workflow sets all non-secret database, logging and publishing controls
inline. It never runs `env`, `printenv`, `set -x`, or an echo of a secret.

## 4. Least privilege

```yaml
permissions:
  contents: read
```

No write token, package, issues, or pull-request permission is requested.
`actions/checkout` uses `persist-credentials: false` so the repository token is
not left in `.git/config` for later steps.

The workflow has no `pull_request` trigger, so repository secrets cannot be
exposed to pull requests from forks.

## 5. Action pin verification

The template pins to official release commits:

```text
actions/checkout      11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
actions/setup-python  a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
```

This preparation phase was not allowed to access GitHub remotely. **Re-verify
the pins before activation:**

```bash
git ls-remote https://github.com/actions/checkout refs/tags/v4.2.2
git ls-remote https://github.com/actions/setup-python refs/tags/v5.6.0
```

The SHA printed by `git ls-remote` must match the template. If a tag moved or a
new patch release is required, update the SHA and keep the tag comment. Review
the release notes before changing pins.

## 6. Activation procedure

Do not run this until the readiness checklist is signed off:

```powershell
Copy-Item deployment\worker.yml.template .github\workflows\publishing-worker.yml
git add .github/workflows/publishing-worker.yml
git commit -m "Activate production publishing worker"
git push
```

Then:

1. Confirm the workflow appears under **Actions → Publishing worker**.
2. Run **Run workflow** once with `confirm=run`. A non-confirming value makes the
   job skip.
3. Read the run log. It must not contain the bot token or database password.
4. Confirm the worker reports `admitted=True`, a sane `charged` value and
   `stopped=idle` or another expected reason.
5. Leave the schedule enabled only after the controlled test in
   `PHASE3_READINESS.md` passes.

## 7. What one run does

`python -m app.worker`:

1. Reaps abandoned accounting rows older than the platform timeout plus grace.
2. Recovers expired job leases with token fencing.
3. Admits one worker run against the 114,000-second internal monthly cap.
4. Claims jobs in priority order and sends through the global pacing permit.
5. Maintains the 180-second lease with a 30-second heartbeat.
6. Stops at 950 attempts, the 50-minute work soft deadline, the 52-minute wall
   soft deadline, or the 58-minute hard deadline.
7. Finishes the worker accounting row and closes the bot session and pool.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Admitted and completed a bounded pass |
| `2` | `DATABASE_BACKEND` was not `postgres` |
| `3` | Monthly accounting cap refused admission |

A non-zero exit marks the Actions job failed, which is intentional.

## 8. Billing and free-tier limits

- GitHub Actions Free for a **public** repository is effectively unlimited for
  this workflow.
- A **private** repository on the Free plan includes 2,000 Linux minutes/month.
  A daily 60-minute job is about 1,800 minutes/month, leaving about 200 minutes
  for manual runs and other workflows.
- Each manual dispatch consumes minutes too. Avoid repeated test dispatches near
  the end of the billing month.
- The workflow is deliberately not triggered on push. Every push-triggered run
  would consume minutes and could publish unexpectedly.

## 9. Deactivation and emergency stop

1. Disable the workflow in **Actions → Publishing worker → ... → Disable
   workflow**.
2. Or remove `.github/workflows/publishing-worker.yml` and push the removal.
3. A running job can be cancelled from the Actions UI, but cancellation does not
   undo an in-flight Telegram send. Pause publishing first if the bot is
   responsive.

See `ROLLBACK.md` for the full emergency sequence.

## Publishing the migration branch from Windows

The clean bundle includes `PUBLISH_TO_GITHUB.ps1`. Extract the ZIP, open PowerShell in the extracted folder, and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\PUBLISH_TO_GITHUB.ps1
```

The script clones `main` into a temporary directory, creates `migration/phase-1-postgres-adapter`, copies only clean project files, checks for local secrets/databases/logs, commits, and pushes that branch. It does not modify `main`, create a pull request, deploy to Vercel, activate GitHub Actions, or register a Telegram webhook. It requires Git for Windows and an authenticated GitHub session (browser sign-in or Git Credential Manager).

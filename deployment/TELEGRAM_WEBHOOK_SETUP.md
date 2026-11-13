# Telegram Webhook Setup

Register the webhook only after Vercel and Supabase are ready. **Do not run the
registration commands during Phase 3 preparation.** They are the activation
runbook.

## 1. Generate the webhook secret

Telegram requires `secret_token` to contain only `A-Z`, `a-z`, `0-9`, `_` and
`-`, length 1–256. Generate it locally:

```powershell
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

Store the value as the Vercel environment variable `TELEGRAM_WEBHOOK_SECRET`.
Never reuse the bot token as the webhook secret. Never put it in a URL, a commit,
or a Telegram message.

## 2. Confirm the endpoint first

```text
GET https://PROJECT.vercel.app/api/telegram
```

Expected:

```json
{"status":"ok","mode":"webhook"}
```

Then confirm the wrong-secret path is closed:

```powershell
Invoke-WebRequest `
  -Method Post `
  -Uri "https://PROJECT.vercel.app/api/telegram" `
  -ContentType "application/json" `
  -Body '{"update_id":1}'
```

Expected: HTTP 403. This request has no secret header, so it must not reach the
dispatcher.

## 3. Register the webhook

PowerShell:

```powershell
$env:TELEGRAM_BOT_TOKEN = "REPLACE_WITH_BOT_TOKEN"
$env:TELEGRAM_WEBHOOK_SECRET = "REPLACE_WITH_WEBHOOK_SECRET"

$body = @{
  url                  = "https://PROJECT.vercel.app/api/telegram"
  secret_token         = $env:TELEGRAM_WEBHOOK_SECRET
  allowed_updates      = @("message", "channel_post", "chat_join_request")
  drop_pending_updates = $false
  max_connections      = 40
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/setWebhook" `
  -ContentType "application/json" `
  -Body $body

Remove-Item Env:TELEGRAM_BOT_TOKEN
Remove-Item Env:TELEGRAM_WEBHOOK_SECRET
```

curl equivalent:

```bash
curl --fail-with-body \
  --request POST \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --header "Content-Type: application/json" \
  --data '{
    "url": "https://PROJECT.vercel.app/api/telegram",
    "secret_token": "REPLACE_WITH_WEBHOOK_SECRET",
    "allowed_updates": ["message", "channel_post", "chat_join_request"],
    "drop_pending_updates": false,
    "max_connections": 40
  }'
```

Expected response:

```json
{"ok":true,"result":true,"description":"Webhook was set"}
```

## 4. Verify

```powershell
$env:TELEGRAM_BOT_TOKEN = "REPLACE_WITH_BOT_TOKEN"
Invoke-RestMethod `
  "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/getWebhookInfo"
Remove-Item Env:TELEGRAM_BOT_TOKEN
```

Expected fields:

- `url` exactly matches the Vercel endpoint.
- `pending_update_count` is small and decreasing.
- `last_error_message` is absent.
- `allowed_updates` includes `message`, `channel_post`, `chat_join_request`.
- `ip_address`/`last_error_date` should not show persistent failures.

If `last_error_message` reports 403, the secret in `setWebhook` does not match
`TELEGRAM_WEBHOOK_SECRET` in Vercel. If it reports 500, inspect Vercel logs and
the Supabase pooler DSN.

## 5. Secret-header implementation

`app/webhook.py` checks:

```text
X-Telegram-Bot-Api-Secret-Token: <TELEGRAM_WEBHOOK_SECRET>
```

The comparison uses `hmac.compare_digest`. A missing or wrong header returns 403
without constructing the database application. The header is not logged.

## 6. What the webhook does and does not do

Allowed inside Vercel:

- parse and validate one update,
- persist source message metadata,
- validate admin commands,
- enqueue manual/resume/scheduled requests,
- send short Telegram admin replies.

Not allowed inside Vercel:

- copy/publish queued content,
- run APScheduler,
- start long polling,
- write SQLite files,
- wait for a long send or sleep.

On PostgreSQL, `/publishnow` and `/resume` create durable rows. The GitHub
Actions worker performs the actual publishing.

## 7. Controlled test without real messages

The webhook path is covered by `tests/test_phase3_webhook.py` using a fake
dispatcher. It verifies the secret header, content type, body limit, invalid
updates, health check, background-task flush and 500 retry behavior without
contacting Telegram or PostgreSQL.

The end-to-end test with a separate Telegram test destination is described in
`PHASE3_READINESS.md`. Perform it only during the authorized activation window,
and only after a test dance destination has been configured.

## 8. Delete the webhook (pause or rollback)

```powershell
$env:TELEGRAM_BOT_TOKEN = "REPLACE_WITH_BOT_TOKEN"
Invoke-RestMethod `
  -Method Post `
  -Uri "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/deleteWebhook" `
  -Body @{ drop_pending_updates = $false }
Remove-Item Env:TELEGRAM_BOT_TOKEN
```

After deletion, no updates are delivered to Vercel. The bot stops responding
until a new webhook is registered or the local polling runtime is started.

## 9. Switching back to polling

1. `deleteWebhook`.
2. Disable the GitHub Actions worker.
3. Set `DATABASE_BACKEND=sqlite` and a persistent `DATABASE_PATH` on the local or
   VPS runtime.
4. Run `python -m app`.
5. Confirm no other process is polling the same bot token.

PostgreSQL data is not automatically copied into SQLite. Read `ROLLBACK.md`
before switching backends.

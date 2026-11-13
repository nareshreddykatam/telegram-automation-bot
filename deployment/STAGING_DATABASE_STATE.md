# Staging Database State — 2026-11-13

This file records the staging setup performed against the dedicated Supabase
project `telegram-automation` (`ymfkfqboffcgagnltqdr`, Mumbai region).

## Applied migrations

The Supabase migration history contains:

1. `001_baseline`
2. `002_phase2_queue`
3. `secure_public_bot_tables`
4. `index_foreign_keys`

The application migration runner uses a separate `public.schema_migrations`
ledger. That ledger has versions 1–4 seeded with the SHA-256 checksums of the
matching files in `app/db/migrations_pg/`. The expected dry-run result is zero
pending migrations, provided those files are unchanged.

## Security state

- RLS is enabled on all 17 application tables and on `schema_migrations`.
- No RLS policies are created for browser/API roles; this is intentional.
- `anon` and `authenticated` have no table privileges on the bot tables and no
  sequence privileges for the bot tables.
- The `postgres` database role retains administrative access and bypasses RLS.
- No dedicated least-privilege runtime login role has been created yet.

## Verification

- Project status: `ACTIVE_HEALTHY`.
- PostgreSQL: 17.11.
- `schema_migrations`: four versions, each checksum is 64 hex characters.
- `anon` and `authenticated` cannot select from the protected tables.
- Supabase security advisor: only informational `rls_enabled_no_policy`
  findings, expected for this server-only access model.
- Performance advisor: no missing foreign-key-index warning after migration 4;
  unused-index notices are expected before application workload exists.

## Still required before deployment

1. Create a dedicated runtime login role with only required DML/sequence access.
2. Verify the exact transaction-pooler DSN and test connectivity from the app.
3. Set Vercel and GitHub Actions secrets/variables without exposing them in logs.
4. Publish the reviewed code to the GitHub migration branch and create a PR.
5. Deploy a preview and test the webhook without registering the production bot.
6. Run the controlled end-to-end test with the separate test bot and private test
   channel; only then consider production webhook registration and worker schedule.

No Vercel deployment, GitHub Actions workflow activation, Telegram webhook
registration, or Telegram publishing was performed during this setup.

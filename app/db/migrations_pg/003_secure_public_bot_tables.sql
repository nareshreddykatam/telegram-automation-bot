-- 003_secure_public_bot_tables.sql
--
-- The bot uses a trusted server-side PostgreSQL connection. No browser/client
-- role should read or mutate these internal operational tables. RLS is kept
-- enabled as defense in depth; no anon/authenticated policies are created.
--
-- Runtime connections must use a trusted backend role (e.g. postgres or a
-- dedicated bot_runtime role). Do not expose service_role credentials.

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;

-- Keep future objects created by the migration owner private from API roles.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL PRIVILEGES ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL PRIVILEGES ON SEQUENCES FROM anon, authenticated;

ALTER TABLE public.admins ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_chats ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.destinations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.publishing_queues ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.publishing_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.publishing_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.moderation_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.moderation_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.join_request_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scheduler_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scheduled_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.publish_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.publishing_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.publish_request_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.worker_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.telegram_rate_limit ENABLE ROW LEVEL SECURITY;

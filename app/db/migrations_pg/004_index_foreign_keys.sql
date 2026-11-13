-- 004_index_foreign_keys.sql
-- Add covering indexes for foreign keys identified by Supabase's performance advisor.

CREATE INDEX IF NOT EXISTS idx_publish_requests_destination_id
  ON public.publish_requests(destination_id);
CREATE INDEX IF NOT EXISTS idx_publishing_history_source_message_id
  ON public.publishing_history(source_message_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_runs_destination_id
  ON public.scheduled_runs(destination_id);

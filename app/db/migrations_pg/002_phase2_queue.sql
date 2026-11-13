-- 002_phase2_queue.sql
--
-- PostgreSQL-only Phase 2 queue, worker lease and pacing schema.
-- SQLite keeps migration 1 and its existing inline publishing behaviour.
--
-- Design notes:
--   * Queue/worker timestamps are TIMESTAMPTZ. Existing application timestamp
--     columns remain TEXT so the Phase 1 repositories stay byte-for-byte
--     compatible.
--   * publishing_runs.uncertain_count already exists in 001_baseline.sql.
--     It is deliberately not added again here.
--   * The active manual/resume index includes "running" so a second
--     /publishnow or /resume coalesces onto the active cycle. The separate
--     one-running-job-per-destination index remains the stricter invariant.

CREATE TABLE IF NOT EXISTS publish_requests (
    id TEXT PRIMARY KEY,
    request_type TEXT NOT NULL CHECK (
        request_type IN ('manual', 'resume', 'schedule', 'catchup')
    ),
    requested_by BIGINT,
    destination_id BIGINT REFERENCES destinations(id) ON DELETE CASCADE,
    schedule_date TEXT,
    slot TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending', 'processing', 'completed',
            'completed_with_errors', 'failed', 'cancelled'
        )
    ),
    planned_jobs INTEGER NOT NULL DEFAULT 0 CHECK (planned_jobs >= 0),
    completed_jobs INTEGER NOT NULL DEFAULT 0 CHECK (completed_jobs >= 0),
    failed_jobs INTEGER NOT NULL DEFAULT 0 CHECK (failed_jobs >= 0),
    cancelled_jobs INTEGER NOT NULL DEFAULT 0 CHECK (cancelled_jobs >= 0),
    cancelled_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    error TEXT,
    CHECK (
        (
            request_type IN ('schedule', 'catchup')
            AND schedule_date IS NOT NULL
            AND slot IS NOT NULL
        )
        OR (
            request_type IN ('manual', 'resume')
            AND schedule_date IS NULL
            AND slot IS NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS publishing_jobs (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES publish_requests(id) ON DELETE CASCADE,
    destination_id BIGINT NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL CHECK (
        trigger IN ('manual', 'resume', 'schedule', 'catchup')
    ),
    schedule_date TEXT,
    slot TEXT,
    dedupe_key TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending', 'running', 'retry', 'completed',
            'completed_with_errors', 'failed', 'cancelled'
        )
    ),
    priority SMALLINT NOT NULL DEFAULT 100 CHECK (priority >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    max_retries INTEGER NOT NULL DEFAULT 5 CHECK (max_retries >= 0),
    claimed_by TEXT,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    send_started_at TIMESTAMPTZ,
    run_id TEXT,
    outcome TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    CHECK (
        (
            trigger IN ('schedule', 'catchup')
            AND schedule_date IS NOT NULL
            AND slot IS NOT NULL
            AND dedupe_key IS NOT NULL
        )
        OR (
            trigger IN ('manual', 'resume')
            AND schedule_date IS NULL
            AND slot IS NULL
            AND dedupe_key IS NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS publish_request_jobs (
    request_id TEXT NOT NULL REFERENCES publish_requests(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES publishing_jobs(id) ON DELETE CASCADE,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (request_id, job_id)
);

CREATE TABLE IF NOT EXISTS worker_runs (
    id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'completed', 'failed', 'abandoned')
    ),
    month_key TEXT NOT NULL CHECK (month_key ~ '^[0-9]{4}-[0-9]{2}$'),
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    setup_seconds INTEGER NOT NULL DEFAULT 120 CHECK (setup_seconds >= 0),
    work_seconds INTEGER NOT NULL DEFAULT 0 CHECK (work_seconds >= 0),
    claimed_jobs INTEGER NOT NULL DEFAULT 0 CHECK (claimed_jobs >= 0),
    error TEXT,
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE IF NOT EXISTS telegram_rate_limit (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    next_allowed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    permit_token UUID,
    permit_expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO telegram_rate_limit (id)
VALUES (1)
ON CONFLICT (id) DO NOTHING;

ALTER TABLE publishing_history ADD COLUMN IF NOT EXISTS unit_id UUID;
ALTER TABLE publishing_history ADD COLUMN IF NOT EXISTS lease_token UUID;

CREATE UNIQUE INDEX IF NOT EXISTS uq_publishing_jobs_running_destination
    ON publishing_jobs(destination_id) WHERE status = 'running';

-- A second manual/resume request coalesces onto a pending, retry or running
-- manual cycle instead of starting a second publishing cycle.
CREATE UNIQUE INDEX IF NOT EXISTS uq_publishing_jobs_active_manual_destination
    ON publishing_jobs(destination_id)
    WHERE trigger IN ('manual', 'resume')
      AND status IN ('pending', 'retry', 'running');

CREATE INDEX IF NOT EXISTS idx_publishing_jobs_claim
    ON publishing_jobs(status, available_at, priority, schedule_date, created_at)
    WHERE status IN ('pending', 'retry');

CREATE INDEX IF NOT EXISTS idx_publishing_jobs_lease
    ON publishing_jobs(lease_expires_at)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_publishing_jobs_request
    ON publishing_jobs(request_id);

CREATE INDEX IF NOT EXISTS idx_publish_requests_status
    ON publish_requests(status, created_at);

CREATE INDEX IF NOT EXISTS idx_prj_job
    ON publish_request_jobs(job_id);

CREATE INDEX IF NOT EXISTS idx_worker_runs_month
    ON worker_runs(month_key, status);

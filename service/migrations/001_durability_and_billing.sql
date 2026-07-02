-- Migration 001 — durability, metering, and takedown tables.
-- Apply to the Supabase project referenced by SUPABASE_URL.
-- The service degrades gracefully if these are absent (best-effort writes),
-- but you want them in production for redeploy-safe jobs, quota, and takedowns.

-- Durable job status — so the RECORD of an audit survives a redeploy even
-- though the in-memory JOBS entry does not (persistence.save_job_status).
create table if not exists audit_jobs (
    audit_id      text primary key,
    url           text,
    status        text,                 -- queued | running | completed | error
    error         text,
    submitted_at  timestamptz,
    completed_at  timestamptz,
    result_summary jsonb,
    updated_at    timestamptz not null default now()
);
create index if not exists audit_jobs_status_idx on audit_jobs (status);

-- Per-API-key monthly usage counter (billing.check_and_meter). key_id is a
-- non-reversible hash of the API key — the raw key is never stored.
create table if not exists api_usage (
    key_id     text not null,
    month      text not null,           -- 'YYYY-MM'
    count      int  not null default 0,
    updated_at timestamptz not null default now(),
    primary key (key_id, month)
);

-- Durable domain suppression / takedown list (persistence.persist_suppression).
create table if not exists suppressed_domains (
    domain     text primary key,        -- registrable host, e.g. 'example.com'
    reason     text,
    created_at timestamptz not null default now()
);

-- NOTE: these tables are written with the service-role key (RLS bypassed).
-- If you enable RLS, add policies that permit the service role, or keep RLS
-- off for these internal tables and never expose them via the anon key.

-- kimimeter schema. Bump PARSER_VERSION env var to invalidate all rows.

CREATE TABLE IF NOT EXISTS projects (
  project_id    TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  file_key            TEXT PRIMARY KEY,
  project_id          TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  session_id          TEXT NOT NULL,
  is_main             BOOLEAN NOT NULL,
  r2_etag             TEXT NOT NULL,
  r2_size_bytes       BIGINT NOT NULL,
  r2_last_modified    TIMESTAMPTZ NOT NULL,
  parsed_at           TIMESTAMPTZ NOT NULL,
  parser_version      TEXT NOT NULL,
  ctx_turns           JSONB NOT NULL DEFAULT '[]'::jsonb,
  turn_count          INT   NOT NULL DEFAULT 0,
  rate_limit_hits     JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS files_project_idx ON files (project_id);
CREATE INDEX IF NOT EXISTS files_session_idx ON files (session_id);
CREATE INDEX IF NOT EXISTS files_modified_idx ON files (r2_last_modified);

CREATE TABLE IF NOT EXISTS records (
  file_key                TEXT NOT NULL REFERENCES files(file_key) ON DELETE CASCADE,
  line_num                INT  NOT NULL,
  uuid                    TEXT,  -- message_id from StatusUpdate
  ts                      TIMESTAMPTZ,
  model                   TEXT NOT NULL DEFAULT 'kimi-k2-6',
  fresh_tokens            BIGINT NOT NULL DEFAULT 0,   -- input_other
  cache_creation_tokens   BIGINT NOT NULL DEFAULT 0,   -- input_cache_creation
  cache_read_tokens       BIGINT NOT NULL DEFAULT 0,   -- input_cache_read
  output_tokens           BIGINT NOT NULL DEFAULT 0,
  cost_usd                NUMERIC(12,6) NOT NULL DEFAULT 0,
  text_chars              BIGINT NOT NULL DEFAULT 0,
  reply_latency_s         NUMERIC(10,3),
  PRIMARY KEY (file_key, line_num)
);

-- Idempotent migration for existing DBs.
ALTER TABLE records ADD COLUMN IF NOT EXISTS
  text_chars BIGINT NOT NULL DEFAULT 0;
ALTER TABLE records ADD COLUMN IF NOT EXISTS
  reply_latency_s NUMERIC(10,3);

-- Cross-file uuid dedup, resolved at INGEST instead of on every read.
-- `records` is immutable between ingests, but every read endpoint was
-- re-running DISTINCT ON (uuid) over the whole table -- a full sort to
-- drop a few percent of duplicates, per request, per range, per project.
-- This flag marks the row that DISTINCT ON (uuid) ORDER BY uuid, file_key,
-- line_num would have kept; readers filter on it instead of sorting.
-- Recomputed by ingest.recompute_canonical() after every ingest, since
-- adding or removing a FILE can change which row wins for a uuid.
--
-- Defaults to TRUE so a freshly-migrated DB over-counts nothing before
-- the first recompute -- it behaves exactly like the pre-dedup state
-- (every row kept) rather than silently dropping rows.
ALTER TABLE records ADD COLUMN IF NOT EXISTS
  is_canonical BOOLEAN NOT NULL DEFAULT TRUE;

-- Pre-aggregated usage, rebuilt at ingest (ingest.rebuild_rollup).
--
-- `records` only changes when an ingest runs, but every dashboard request
-- was re-deriving the entire history from raw rows: several separate full
-- aggregate passes over every canonical record, per request, per range,
-- per project. This holds the same numbers already summed.
--
-- Grain is (session_id, hour, model) deliberately:
--   * keyed by HOUR, not by session, so a range filter sums only the
--     in-range hours -- a session straddling the boundary would otherwise
--     be counted whole. Sums, counts and min/max ts all compose exactly.
--   * carrying MODEL means a session's dominant model is argmax(requests)
--     over the in-range rows -- exact, not an approximation of MODE().
-- What does NOT compose is PERCENTILE_CONT, so response-size p50/p90 is
-- still computed live from records (see /api/dashboard).
CREATE TABLE IF NOT EXISTS usage_rollup (
  session_id          TEXT        NOT NULL,
  project_id          TEXT        NOT NULL,
  hour                TIMESTAMPTZ NOT NULL,
  model               TEXT        NOT NULL,
  is_main             BOOLEAN     NOT NULL DEFAULT TRUE,
  first_ts            TIMESTAMPTZ NOT NULL,
  last_ts             TIMESTAMPTZ NOT NULL,
  requests            BIGINT      NOT NULL DEFAULT 0,
  fresh_tokens        BIGINT      NOT NULL DEFAULT 0,
  output_tokens       BIGINT      NOT NULL DEFAULT 0,
  cache_read_tokens   BIGINT      NOT NULL DEFAULT 0,
  cost_usd            NUMERIC(18,8) NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, hour, model, is_main)
);
CREATE INDEX IF NOT EXISTS usage_rollup_hour_idx ON usage_rollup (hour);
CREATE INDEX IF NOT EXISTS usage_rollup_project_idx ON usage_rollup (project_id, hour);

-- Pre-aggregated tool calls, rebuilt at ingest alongside usage_rollup.
-- Serves /api/tool-usage and /api/tool-error-rate, which were the last
-- endpoints still scanning a raw table per request (tool_uses, joined to
-- files for the project).
--
-- No `model` dimension, unlike usage_rollup: in Kimi wire.jsonl
-- tool_uses.line_num and records.line_num are disjoint (ToolCall lines vs
-- StatusUpdate lines), so there is no per-tool-call model to join to.
-- Both endpoints already treat model as a whole-dataset constant.
--
-- The two endpoints count DIFFERENT populations, so both are stored:
--   n_total  every tool_use in the group           -> /api/tool-usage
--   n_rated  those with is_error NOT NULL (settled
--            calls, which is all tool_error_rate
--            counts)                               -> denominator
--   n_error  of those, the ones that errored       -> numerator
CREATE TABLE IF NOT EXISTS tool_rollup (
  hour        TIMESTAMPTZ NOT NULL,
  project_id  TEXT        NOT NULL,
  tool_name   TEXT        NOT NULL,
  n_total     BIGINT      NOT NULL DEFAULT 0,
  n_rated     BIGINT      NOT NULL DEFAULT 0,
  n_error     BIGINT      NOT NULL DEFAULT 0,
  lines_added   BIGINT    NOT NULL DEFAULT 0,
  lines_deleted BIGINT    NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, project_id, tool_name)
);
CREATE INDEX IF NOT EXISTS tool_rollup_hour_idx ON tool_rollup (hour);
CREATE INDEX IF NOT EXISTS tool_rollup_project_idx ON tool_rollup (project_id, hour);

-- Pre-aggregated reply-latency bands + outlier dots for /api/reply-latency.
--
-- Percentiles do NOT compose across buckets, so unlike usage_rollup this
-- cannot be stored at one fine grain and summed up. What makes it
-- precomputable anyway is that the display buckets are epoch-aligned and
-- deterministic -- floor(epoch / bucket_s) * bucket_s does not move with
-- `now` -- and the UI only ever uses a fixed set of widths. So the
-- percentiles are computed once PER bucket_s and a range filter merely
-- selects which buckets to return. Exact, not approximate.
-- The sub-hour widths (the 24h view) are excluded: they would need a row
-- per 5 minutes of all history to serve one day, so those ranges keep the
-- live path.
--
-- project_id = '' is the ALL-PROJECTS row. It has to be stored
-- separately because a filter changes the population inside each
-- (bucket, model) group, and p50 over all projects is not derivable from
-- the per-project p50s. The model filter needs no such treatment -- the
-- rows are already grouped by model, so filtering selects whole groups.
--
-- `outliers` holds the top/bottom 1% dots the panel draws, as
-- [{ts, latency_s, file_key, line_num}], only for buckets with n >= 100
-- (1% of fewer would just be the min/max).
CREATE TABLE IF NOT EXISTS latency_rollup (
  bucket_s    INTEGER     NOT NULL,
  bucket      TIMESTAMPTZ NOT NULL,
  project_id  TEXT        NOT NULL,
  model       TEXT        NOT NULL,
  n           BIGINT      NOT NULL,
  p10         DOUBLE PRECISION,
  p50         DOUBLE PRECISION,
  p90         DOUBLE PRECISION,
  outliers    JSONB       NOT NULL DEFAULT '[]'::jsonb,
  PRIMARY KEY (bucket_s, bucket, project_id, model)
);
CREATE INDEX IF NOT EXISTS latency_rollup_lookup_idx
  ON latency_rollup (bucket_s, project_id, bucket);

-- Only a minority of records carry a reply_latency_s; a partial covering
-- index keeps the live path (and the rollup build) off the rest.
CREATE INDEX IF NOT EXISTS records_latency_idx ON records (ts)
  INCLUDE (model, reply_latency_s, file_key, line_num)
  WHERE reply_latency_s IS NOT NULL;

CREATE INDEX IF NOT EXISTS records_uuid_idx ON records (uuid) WHERE uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS records_ts_idx ON records (ts);
-- Every read endpoint filters `is_canonical AND ts >= ...`.
CREATE INDEX IF NOT EXISTS records_canonical_ts_idx
  ON records (ts) WHERE is_canonical;
CREATE INDEX IF NOT EXISTS records_model_idx ON records (model);

CREATE TABLE IF NOT EXISTS tool_uses (
  file_key   TEXT NOT NULL REFERENCES files(file_key) ON DELETE CASCADE,
  line_num   INT  NOT NULL,
  idx        INT  NOT NULL,
  ts         TIMESTAMPTZ,
  tool_name  TEXT NOT NULL,
  is_error   BOOLEAN,
  lines_added   BIGINT NOT NULL DEFAULT 0,
  lines_deleted BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (file_key, line_num, idx)
);

-- Idempotent migration for existing DBs. Line churn rides tool_uses
-- (same grain: one row per tool call) and tool_rollup (the hourly sums
-- /api/dashboard's Lines Added/Deleted panels read). Populated at parse
-- time from edit/write tool-call args — the wire's tool results carry
-- no diff — so historical rows stay 0 until a PARSER_VERSION bump
-- reparses them.
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS
  lines_added BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS
  lines_deleted BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tool_rollup ADD COLUMN IF NOT EXISTS
  lines_added BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tool_rollup ADD COLUMN IF NOT EXISTS
  lines_deleted BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS tool_uses_ts_idx   ON tool_uses (ts);
CREATE INDEX IF NOT EXISTS tool_uses_tool_idx ON tool_uses (tool_name);

CREATE TABLE IF NOT EXISTS ingest_runs (
  id              BIGSERIAL PRIMARY KEY,
  started_at      TIMESTAMPTZ NOT NULL,
  finished_at     TIMESTAMPTZ,
  trigger         TEXT NOT NULL,
  r2_listed       INT,
  reparsed        INT,
  inserted        INT,
  deleted         INT,
  error           TEXT
);

-- Transcripts seen and deliberately not ingested: another harness's key
-- layout, or bytes in a format this parser does not handle yet. Counted
-- rather than persisted, so they are visible without being marked parsed.
ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS skipped INT;

# kimimeter

@README.md

## Repo orientation

- `backend/` — FastAPI app.
  - `app.py` — startup/shutdown, route mounting (login + api +
    api_dashboard + api_sessions), `/` static, asset cache-bust.
  - `api.py` — shared query helpers (_proj_*, _parse_range,
    _bucket_seconds, Phases, _iso) plus the tool/activity/latency/
    models/projects/cache endpoints (`/api/me`, `/api/tool-usage`,
    `/api/tool-error-rate`, `/api/activity-heatmap`, `/api/reply-latency`,
    `/api/models`, `/api/projects`, `/api/cache`, `/api/events` SSE).
  - `api_dashboard.py` — `/api/dashboard`, split out of api.py
    (_DashQuery/_DashRows pipeline).
  - `api_sessions.py` — `/api/sessions*`, `/api/context-growth/{agg,session}`,
    split out of api.py.
  - `bash_churn.py` — lines_added/lines_deleted recovered from Bash and
    Shell command text; see the churn note under the schema below.
  - `parse.py` — wire.jsonl → records + ctx_turns. Mirrors
    the canonical `~/.kimi-code/scripts/parse_wire.py` for turn-based
    StatusUpdate extraction.
    `parse_file` raises `UnsupportedTranscriptError` on a Claude Code
    transcript rather than returning an empty parse, which the ingest would
    persist as a successfully parsed file.
  - `pricing.py` — single source of truth for Kimi K2.6 / K2.7 Code / K3 rates.
    Bump `PARSER_VERSION` in `.env` whenever this changes.
  - `ingest.py` — R2 walk, etag/parser-version reparse decision, persistence
    in two-phase transactions, broadcasts `ingest_done` SSE on success.
    Foreign transcripts — a Claude Code session that claude-code-proxy ran
    through Kimi, so archive_sessions.py routed it to this
    bucket under Claude's own key layout — are counted into the run's
    `skipped` and never persisted. No `files` row means no parser_version
    stamp, so the release that adds a parser ingests them with no backfill.
  - `r2.py` — S3 client with `file://` filesystem-mirror fallback for dev.
  - `auth.py`, `login.py`, `session.py` — PBKDF2 verification against the
    external auth DB's `users.config`, HMAC-signed session cookies, plus
    a guest-mode sentinel (`user_id=0`, per-process secret).
  - `events.py` — thread-safe SSE broadcaster.
  - `db.py` — two psycopg pools: `viz_pool` (kimimeter) and `auth_pool`
    (read-only auth DB). Pools never join across DBs.
    `close_viz_pool()` is the test-teardown hook that drops the cached
    viz pool after repointing DATABASE_URL_VIZ. `sql_literal()` marks
    trusted f-string SQL for psycopg's LiteralString-typed execute().
  - `cache.py` — in-memory LRU for raw transcript bytes.
  - `schema.sql` — idempotent `CREATE TABLE IF NOT EXISTS` + safe
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations.

- `public/` — `index.html`, `app.css`. Served at `/`. Backend rewrites
  `index.html` on each request to inject `window.BACKEND_URL`,
  `window.IS_GUEST`, and mtime-based `?v=` query strings on every static
  asset reference.

- `src/` — React JSX modules served at `/src/*` (in-browser Babel, no
  build step).
  - `app.jsx` — top-level shell, routing, dashboard fetcher, SSE listener.
  - `parser.js` — in-browser wire.jsonl parser used by the Inspector.
    Pricing table and time-based model assignment here MUST match
    `backend/pricing.py` and `backend/parse.py`.
  - `dashboard-charts.jsx`, `dashboard-charts-extra.jsx` — SVG panels.
  - `views/` — `cache-view.jsx`, `context-growth-view-v2.jsx`.

- `scripts/` — symlinks to canonical `~/.kimi-code/scripts/*.py`. **Read-only**;
  the web app does NOT invoke them at runtime.

- `tests/` — pytest suite.

- `fixtures/` — small JSONL + zip samples for parser and API tests.

## Conventions

- **Cost uses Kimi K2.6 or K2.7 Code rates**. Sessions whose first event is
  before the hardcoded cutoff are labelled `kimi-k2-6`; newer sessions are
  `kimi-k2-7-code`. `cache_creation` is billed at a flat rate (no TTL split in
  Kimi wire format).
- **SV-CANONICAL-FLAG — cross-file uuid dedup is resolved at INGEST.**
  `records.is_canonical` marks the row that
  `DISTINCT ON (uuid) ORDER BY uuid, file_key, line_num` would have kept;
  NULL-uuid rows are always canonical. `ingest.recompute_canonical()`
  rebuilds it after every successful ingest, because adding or removing a
  FILE can change which row wins. Read paths filter the boolean — do NOT
  reintroduce `DISTINCT ON (uuid)` in an endpoint.
- **SV-ROLLUP — `usage_rollup` / `tool_rollup` / `latency_rollup` are
  derived state**, rebuilt at ingest after `recompute_canonical` (they read
  `is_canonical`). Anything that mutates `records` or `tool_uses` outside
  ingest must rebuild them or the endpoints serve stale numbers; the tests
  that insert probe rows do exactly that.
  - Serve from a rollup only what composes: sums, counts, min/max.
    `PERCENTILE_CONT` does not, which is why response-size p50/p90 stays a
    live pass and `latency_rollup` stores bands per display-bucket width
    rather than at one fine grain.
  - `usage_rollup` and `tool_rollup` are hourly, so they can only answer
    display buckets >= 1h. The 24h view buckets at 5 minutes and takes a
    live subquery shaped with the same column names — keep both paths
    behind one set of queries so they cannot drift.
  - `tool_uses` / `tool_rollup` also carry `lines_added` / `lines_deleted`
    — edit churn derived at parse time from Edit/Write (kimi-code) and
    StrReplaceFile/WriteFile (legacy) call ARGS, because the wire's tool
    results carry no diff. Bash and legacy Shell count too, via
    `backend/bash_churn.py`, which reads what their command TEXT
    enumerates directly — heredoc bodies redirected into a file, inline
    git-apply/patch hunks, literal python replacements — and 0 for
    anything that would have to be RUN to measure. They are the majority
    of tool calls, so leaving them at zero hid most of the churn. /api/dashboard serves them as the `churn`
    series behind the Lines Added/Deleted panels. tool_uses has no model
    dimension, so `?model=` does not filter churn (same caveat as the
    tool endpoints).
- **Don't invoke `~/.kimi-code/scripts/parse_wire.py`** at runtime, and
  don't edit it from this repo. If the canonical Python and our port
  drift, fix it here, not there.
- **Tests use fixtures, not real R2.** The R2 client supports
  `R2_ENDPOINT=file:///path/to/mirror/` for offline dev.
- **Parser version invalidation:** Bump `PARSER_VERSION` in `.env` whenever
  parser semantics or `pricing.py` rates change — every file reparses on
  next ingest.
- **In-browser fallback retained:** The drag-drop FileReader path in
  `src/app.jsx` stays as an offline fallback. No upload endpoint exists.

## Operations

- Manual ingest: `POST /admin/ingest` with `X-Admin-Token: $ADMIN_TOKEN`.
- Bump `PARSER_VERSION` in `.env` whenever parser semantics or
  `pricing.py` rates change — every file reparses on next ingest.

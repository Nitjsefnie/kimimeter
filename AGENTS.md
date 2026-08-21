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

## CI — batch your pushes

**There are TEN workflows, not one.** `tests.yml` is the one people
remember, and a green pytest says nothing about the other nine. Six run
locally — run them before pushing, because CI is the backstop, not the
first check:

```bash
python3 -m pytest tests/ -q --cov=backend          # tests.yml (+ coverage)
git ls-files -co --exclude-standard '*.py' | xargs pylint       # lint.yml, gate 1
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle  # lint.yml, gate 2
pyright                                            # types.yml
npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'  # eslint.yml
python3 scripts/ci/smoke.py                        # smoke.yml
pip-audit -r backend/requirements.txt -r requirements-dev.txt \
          -r requirements-test.txt                 # audit.yml
actionlint .github/workflows/*.yml && \
  zizmor .github/workflows/                        # actionlint.yml
```

**Run them against the PINNED deps**, not whatever your interpreter has.
`pyright` resolves third-party types from the installed packages, so a
stale local psycopg makes it disagree with CI:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt -r requirements-dev.txt -r requirements-test.txt
# or point pyright at an existing one:
pyright --pythonpath /path/to/venv/bin/python
```

**`-co --exclude-standard`, not a bare `git ls-files`.** CI lints the
committed tree, so the workflow's own `git ls-files '*.py'` is right
*there*. Locally it is a trap: a brand-new module is untracked until you
stage it, so pylint reports a clean run over every file except the one
you just wrote.

The four that only make sense on GitHub:

| Workflow | Question it answers | Trigger |
| --- | --- | --- |
| `codeql.yml` | Is there a security defect in the Python or JS? Results go to the Security tab, never the build. | push + weekly cron. The cron is NOT redundant: a query published today would otherwise only ever run against files touched after it shipped. Deliberately skips `pull_request_review`, because `analyze` files SARIF against the *event's* SHA while our checkout takes the PR head — two different commits. |
| `audit.yml` | Are the frozen pins still free of advisories? Resolves the full transitive tree, which is the point — nothing here pins `starlette`. | push + **daily** cron. The cron is the important half: this answer changes with no commit to hang it on. |
| `speed.yml` | Did the tests that exist in both this commit and the last release get >30% slower? | push. Runs BOTH builds on the same runner, interleaved, min-of-rounds. Skips green while no release exists. |
| `release.yml` | — | push to `master` touching `VERSION`. Waits for every other check on that SHA, then tags `v<VERSION>`. |

**Coverage is a ratchet at 82%**, in `tests.yml`, checked by a step of its
own so "tests failed" and "coverage dropped" stay distinguishable. Raise
the floor as coverage climbs; never lower it to turn a build green.

**Release = edit `VERSION`.** One bare semver line at the repo root, no
leading `v`. `release.yml` reacts to it; nothing bumps it automatically,
because deciding patch-vs-minor is a judgement about what changed.
`backend/version.py` reads it and `/health` reports it.

**Actions are hash-pinned**, with the version in a trailing comment. Do
not "tidy" one back to `@v4`: a tag is a moving pointer, and these jobs
hold a repository token. Dependabot keeps the hashes current. Every
workflow also sets `permissions:` explicitly and passes
`persist-credentials: false` to checkout — `zizmor` enforces all three,
and a suppression belongs at the offending line with a justification,
never as a raised `--min-severity`.

**`.gitignore` is deny-by-default**: `*` first, then each shipped path
named back. A new file of an unlisted type is invisible to git and will
NOT appear in `git status` — `git check-ignore -v <path>` names the rule
hiding it, and the fix is a name-back rule in the file's own directory
block. Never "fix" it by loosening the leading `*`.

**Dates in tests must be relative to now.** A fixture pinned to an
absolute timestamp inside a `range=30d` assertion silently leaves the
window as the calendar advances, and then fails for a reason unrelated to
what it checks. That is not hypothetical: the malformed-`ts` test in
`tests/test_api.py` was pinned to 2026-07-20 and started failing on
2026-08-19.

## Operations

- Manual ingest: `POST /admin/ingest` with `X-Admin-Token: $ADMIN_TOKEN`.
- Bump `PARSER_VERSION` in `.env` whenever parser semantics or
  `pricing.py` rates change — every file reparses on next ingest.

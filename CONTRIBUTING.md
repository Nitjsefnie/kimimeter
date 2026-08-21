# Contributing to kimimeter

Issues and pull requests are welcome — especially if your numbers disagree
with ours. This project is a cost- and usage-accounting tool, so a report
that says "your figure is wrong and here is the arithmetic" is the most
valuable thing you can send.

## LLM and agent contributions are welcome

You may use an LLM or a coding agent to write your contribution. There is
no penalty, no separate review queue, and no expectation that you rewrite
its output by hand. Much of this repo was built that way.

Two conditions, and they are about honesty rather than provenance:

1. **Disclose the model** with a trailer on each commit it authored:

   ```
   Co-Authored-By: <Model Name> <noreply@example.com>
   ```

   e.g. `Co-Authored-By: Kimi K2.6 <noreply@kimi.com>`. One primary-author
   trailer per commit.

2. **Do not submit claims you have not verified.** This matters more here
   than in most repos, because plausible-looking cost arithmetic is very
   easy to generate and very hard to spot as wrong. If your PR says a
   change makes ingest faster, or fixes a miscount, paste the command and
   its real output. "Tests pass" without the run is not evidence.

If a maintainer's reply reads like it was drafted by an agent, it probably
was. That is fine in both directions.

### If you are an agent reading this

Read [`AGENTS.md`](AGENTS.md) first — it is the architecture and
conventions brief, written for you. It is authoritative; this file only
covers process.

The rules that reject the most patches:

| Rule | What it forbids |
|---|---|
| Three token classes | Collapsing `fresh` / `read` / `create` into one input rate. They are `input_other`, `input_cache_read`, and `input_cache_creation`, priced separately. |
| No TTL split | Adding a 5m/1h cache-write split. The Kimi wire format carries no TTL, so `cache_create` is billed at a flat rate — do not port Anthropic's tiering in. |
| `PARSER_VERSION` bump | Changing the rate table or the parse algorithm without bumping it. |
| No build step | Adding webpack, vite, or an `npm install`. The frontend transpiles in the browser on purpose. |
| Parameterised SQL | Interpolating a value into a query string instead of passing `%s` parameters. |

Rates and token accounting live in `backend/pricing.py` and are mirrored by
`tests/test_pricing.py`. Change one without the other and the suite tells
you.

## Getting it running

Requires **Python 3.13+** and a local **PostgreSQL** you can create
databases in.

```bash
createdb kimimeter
psql kimimeter -f backend/schema.sql

cp backend/.env.example .env      # then edit: DATABASE_URL_VIZ, R2_*, ADMIN_TOKEN
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt

python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8001
```

No R2 credentials? Point `R2_ENDPOINT` at a directory instead — the client
walks the tree in `file://` mode:

```bash
R2_ENDPOINT=file:///path/to/transcripts/
```

`fixtures/` holds small mirrors you can point at to get a working instance
without real data.

## Tests

```bash
python3 -m pytest tests/ -q --cov=backend   # full suite (+ coverage)
python3 -m pytest tests/test_pricing.py -v
```

CI runs **ten** separate workflows, so a green suite is a small fraction
of the gate. These six you can and should run locally before pushing:

```bash
git ls-files -co --exclude-standard '*.py' | xargs pylint       # lint, gate 1
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle  # lint, gate 2
pyright                                                         # types
npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'            # eslint
python3 scripts/ci/smoke.py                                     # smoke
pip-audit -r backend/requirements.txt -r requirements-dev.txt \
          -r requirements-test.txt                              # audit
```

`pip install -r requirements-dev.txt -r requirements-test.txt` gets the
pinned toolchain. Run these against an environment holding the **pinned**
runtime deps as well (`pip install -r backend/requirements.txt`):
`pyright` resolves third-party types from the installed packages, so a
stale local one disagrees with CI. Point it at a venv with
`pyright --pythonpath /path/to/venv/bin/python` if you keep them separate.

Coverage is gated at 82% — a ratchet set under the current number, not a
target.

The remaining four need GitHub: `codeql` (security analysis, weekly cron),
`audit` (`pip-audit`, daily cron), `actionlint` (lints the workflows
themselves), `speed` (benchmarks this commit against the last release on
the same runner, fails at >30%), and `release` (tags `v<VERSION>` once
every other check on the commit passes).

Use `-co --exclude-standard`, not a bare `git ls-files`: a brand-new
module is untracked until you stage it, and pylint would report a clean
run over every file except the one you just wrote.

**Dates in tests go relative to now.** A fixture pinned to an absolute
timestamp inside a `range=30d` assertion leaves the window as the
calendar advances and then fails for a reason unrelated to what it
checks — which is exactly what happened to the malformed-`ts` test.

The suite creates and drops its own test databases, so it needs a Postgres
your user can `createdb` on. It does not touch your real data and never
contacts R2.

Two tests in `tests/test_ingest.py` are worth knowing about before you
touch ingest:

- `test_pool_and_sequential_ingest_agree` runs the same mirror pooled and
  sequentially and requires the results to match. If you change ingest
  concurrency, that is the test that catches you.
- `test_parser_version_bump_reparses_all` pins the reparse contract below.

## If you change how cost is computed

Bump `PARSER_VERSION`. Every file reparses on the next ingest;
without the bump, stored `cost_usd` values keep the old rates and the
dashboard silently mixes them. Mention the bump in your PR so deployers
know a reparse is coming.

## House style

- **Python** — `from __future__ import annotations` at the top of every
  module. Type hints throughout. Raw SQL via psycopg3, no ORM.
- **SQL** — parameterised (`%s`) always. Never interpolate a value into a
  query string.
- **JS/JSX** — ES2020-ish, React function components, shared helpers hung
  on `window.`. No transpile step beyond in-browser Babel.
- **Naming** — `snake_case` in Python, `camelCase` in JS, singular SQL
  table names.
- There is no linter or formatter config. Match the surrounding file.

## Licensing

kimimeter is MIT (see [`LICENSE`](LICENSE)). If a future change vendors
third-party code, reproduce its notice in a separate `NOTICE` file rather
than appending it to `LICENSE`, which makes GitHub misclassify the
project.

## Pull requests

Small and single-purpose beats large and comprehensive. In the
description, include:

- what changed and why,
- the actual output of the tests you ran,
- for a performance change, a before and after measurement rather than an
  assertion that it should be faster.

A bug report that pins down *where* the arithmetic goes wrong is worth as
much as a patch, and is often easier to review. If you are unsure whether
something is a bug or intended, open an issue and ask.

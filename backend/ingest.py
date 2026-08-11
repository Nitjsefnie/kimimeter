"""R2 → Postgres ingest.

Per-file granularity: every wire.jsonl under bucket root → one row in `files`
+ N rows in `records` (per StatusUpdate). Cross-file uuid dedup is a
query-time concern.

Reparse trigger per FILE: row missing OR etag changed OR parser_version
mismatch. Orphan files (R2 key gone) are deleted. CASCADE drops records.

Kimi R2 layout:
  sessions/{session_hash}/{uuid}/wire.jsonl
  sessions/{session_hash}/{uuid}/context.jsonl
  sessions/{session_hash}/{uuid}/state.json

We ingest only wire.jsonl; project_id = session_hash, session_id = uuid.
"""
from __future__ import annotations

import json
import logging
import lzma
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import NamedTuple

from botocore.exceptions import BotoCoreError, ClientError

from backend import api, api_dashboard, cache, db, events, parse, r2

log = logging.getLogger("kimimeter.ingest")

# What the fetch retry treats as transient. None of the boto3 failures is an
# OSError — ConnectionClosedError and EndpointConnectionError are
# BotoCoreErrors, and ClientError descends from neither — so catching
# OSError alone would miss exactly the drops this retry exists for.
# Deliberately NOT `Exception`: see FatalFetchError.
TRANSIENT_FETCH_ERRORS = (OSError, BotoCoreError, ClientError)

# A corrupt object, not a corrupt connection. r2.get_object inflates `.xz`
# keys transparently, so lzma raises from INSIDE the fetch — and every
# production object is `.xz`, which makes this the likeliest per-object
# failure there is. It is deterministic: retrying cannot un-truncate an
# upload, and treating it as a bug would abort the whole run over one bad
# file, which is the exact failure issue #3 is about.
CORRUPT_PAYLOAD_ERRORS = (lzma.LZMAError, EOFError)


class FatalFetchError(Exception):
    """A non-transient failure of an R2 GET, i.e. a bug rather than a drop.

    Routed past the per-object collector to the run-level handler on
    purpose: it is not something the next hourly run will fix, and booking
    it per object would report a code defect as a partial-data problem.
    """


# Bounded retry for the R2 GET only (see _fetch_with_retry). The tuple is
# the backoff BETWEEN attempts, so this is three attempts sleeping 0.5s then
# 1.0s: long enough to ride out a dropped connection, short enough that a
# genuinely dead object costs 1.5s rather than a run. Attempts are derived
# from the tuple so the two can never disagree.
FETCH_BACKOFF_S = (0.5, 1.0)
FETCH_ATTEMPTS = len(FETCH_BACKOFF_S) + 1

# How many failing keys the run's `error` summary names before it truncates.
# The point is a diagnosable message, not a transcript of every key.
FAILURE_KEYS_IN_SUMMARY = 5


def _begin_run(started: datetime, trigger: str) -> int:
    """Book the ingest_runs row and return its id."""
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_runs (started_at, trigger) VALUES (%s, %s) "
            "RETURNING id",
            (started, trigger),
        )
        row = cur.fetchone()
        if row is None:
            # INSERT ... RETURNING always yields a row; a None here means
            # the driver misbehaved, and continuing would mislabel the run.
            raise RuntimeError("ingest_runs insert returned no id")
        run_id = int(row[0])
        c.commit()
    return run_id


def _existing_files() -> dict:
    """file_key -> (etag, parser_version) for reparse decisions."""
    with db.viz_conn() as c:
        return {
            row[0]: (row[1], row[2])
            for row in c.execute(
                "SELECT file_key, r2_etag, parser_version FROM files"
            ).fetchall()
        }


def _is_foreign_transcript_key(parts: list[str]) -> bool:
    """A transcript written by another harness, in that harness's layout.

    Claude Code sessions land in this bucket whenever claude-code-proxy
    routes one through Kimi: archive_sessions.py picks the bucket from the
    provider marker stamped into the transcript but keeps Claude's own key
    layout, `<project-slug>/<uuid>/<uuid>.jsonl`, with subagent transcripts
    under `<uuid>/data/`. None of that is `sessions/`-prefixed, so it never
    reaches the parser — counting it is what makes the skip a deliberate,
    reportable decision instead of an invisible one.

    The depth floor keeps flat bucket-root objects (`user-history/*.jsonl`)
    out of the count: those are not session transcripts.
    """
    return (parts[0] != "sessions" and len(parts) >= 3
            and parts[-1].endswith((".jsonl", ".jsonl.xz")))


class _Scan(NamedTuple):
    """What one walk of the bucket found."""
    wire_objs: list
    marker_items: list[tuple[str, str]]
    # Transcripts in another harness's layout; counted, never fetched.
    foreign: int


def _scan_r2() -> _Scan:
    """Walk R2 once: wire.jsonl objects, project.json markers, foreign count.

    The marker is published by archive_sessions.py on the originating box
    and carries the path the session was run from — we use it as the
    project's display_name.
    """
    wire_objs: list = []
    marker_items: list[tuple[str, str]] = []
    foreign = 0
    for obj in r2.list_keys():
        parts = obj.key.split("/")
        if len(parts) >= 4 and parts[0] == "sessions" \
                and parts[-1] in ("wire.jsonl", "wire.jsonl.xz"):
            wire_objs.append(obj)
        elif len(parts) == 3 and parts[0] == "sessions" \
                and parts[2] == "project.json":
            marker_items.append((parts[1], obj.key))
        elif _is_foreign_transcript_key(parts):
            foreign += 1
    return _Scan(wire_objs, marker_items, foreign)


def _resolve_project_paths(marker_items: list[tuple[str, str]],
                           workers: int, failed: list[tuple[str, str]]
                           ) -> dict[str, str]:
    """Fetch marker bodies on the pool; project_paths must be fully
    populated before the wire-object loop starts."""
    project_paths: dict[str, str] = {}
    if marker_items:
        # A marker GET is as droppable as a wire GET — and failing one
        # used to abort the run even earlier, before r2_listed had been
        # counted. Same collector, same `failed` list, one summary.
        for item, res, exc in _resolve(
            marker_items, lambda it: _fetch_marker(*it), workers
        ):
            if exc is not None:
                _record_failure(failed, item[1], exc)
                continue
            if res is not None:
                project_paths[res[0]] = res[1]
    return project_paths


class _Plan(NamedTuple):
    """What one R2 scan says needs doing."""
    listed: int
    seen_keys: set[str]
    seen_projects: dict[str, dict]
    # (obj, proj, project_id, session_id, is_main, stored) per file
    # needing work; fetched+parsed on a pool.
    todo: list[tuple]


def _update_seen_project(seen_projects: dict[str, dict],
                         project_paths: dict[str, str],
                         project_id: str, last_modified) -> dict:
    proj = seen_projects.setdefault(project_id, {
        "project_id": project_id,
        "display_name": project_paths.get(project_id, project_id),
        "first_seen_at": last_modified,
        "last_seen_at": last_modified,
    })
    if last_modified < proj["first_seen_at"]:
        proj["first_seen_at"] = last_modified
    if last_modified > proj["last_seen_at"]:
        proj["last_seen_at"] = last_modified
    return proj


def _plan_work(wire_objs: list, project_paths: dict[str, str],
               existing: dict, parser_version: str) -> _Plan:
    listed = 0
    seen_keys: set[str] = set()
    seen_projects: dict[str, dict] = {}
    todo: list[tuple] = []
    for obj in wire_objs:
        parts = obj.key.split("/")
        project_id = parts[1]
        session_id = parts[2]
        # Subagent transcripts live at .../subagents/<sub-id>/wire.jsonl;
        # main session transcripts at .../<uuid>/wire.jsonl. Both get
        # ingested, but the classification drives the main/subagent
        # split surfaced by /api/dashboard.
        is_main = "/subagents/" not in obj.key
        listed += 1
        seen_keys.add(obj.key)

        proj = _update_seen_project(
            seen_projects, project_paths, project_id, obj.last_modified
        )

        stored = existing.get(obj.key)
        if (stored is not None and stored[0] == obj.etag
                and stored[1] == parser_version):
            continue
        todo.append((obj, proj, project_id, session_id, is_main, stored))
    return _Plan(listed, seen_keys, seen_projects, todo)


def _persist_one(item: tuple, parsed: dict | None, exc: BaseException | None,
                 failed: list[tuple[str, str]], parser_version: str
                 ) -> str | None:
    """Persist one fetched+parsed file; book the failure instead when the
    fetch raised. Returns "inserted"/"reparsed"/"skipped"/None for the
    counters."""
    obj, proj, project_id, session_id, is_main, stored = item
    if isinstance(exc, parse.UnsupportedTranscriptError):
        # Deliberately NOT booked in `failed`: nothing is wrong and no
        # retry would help, so reporting it as a run error would make
        # every run report one forever. Returning before _persist is the
        # point — no `files` row means the object is not marked parsed,
        # so the run that first understands the format ingests it.
        log.info("ingest: %s skipped: %s", obj.key, exc)
        return "skipped"
    if exc is not None:
        _record_failure(failed, obj.key, exc)
        return None
    _persist(
        obj, proj, project_id, session_id, is_main, parsed, parser_version,
    )
    return "inserted" if stored is None else "reparsed"


def _fetch_and_persist(todo: list[tuple], workers: int, parser_version: str,
                       failed: list[tuple[str, str]]) -> tuple[int, int, int]:
    """Fetch + parse is ~88% of per-file wall time and is network-bound
    (one R2 GET each), so it runs on a thread pool. Persistence stays
    on this thread: the per-file transaction boundary, and therefore
    ordering and failure semantics, are exactly as before. Work is
    submitted in bounded chunks so an 8k-file reparse does not hold
    every inflated blob in memory at once."""
    inserted = 0
    reparsed = 0
    skipped = 0
    chunk = max(1, workers * 4)
    for start in range(0, len(todo), chunk):
        batch = todo[start:start + chunk]
        for item, parsed, exc in _resolve(
            batch, lambda it: _fetch_and_parse(it[0].key), workers
        ):
            outcome = _persist_one(item, parsed, exc, failed, parser_version)
            if outcome == "inserted":
                inserted += 1
            elif outcome == "reparsed":
                reparsed += 1
            elif outcome == "skipped":
                skipped += 1
    return inserted, reparsed, skipped


def _upsert_projects(seen_projects: dict[str, dict]) -> None:
    """Unconditional project upsert — even when no files needed reparse,
    the project marker's path may have changed (e.g. user renamed a
    work_dir), and the per-file persist runs only on reparse.
    Pushing every seen project here keeps display_name fresh."""
    if seen_projects:
        with db.viz_conn() as c, c.cursor() as cur:
            cur.executemany(
                "INSERT INTO projects (project_id, display_name, "
                "first_seen_at, last_seen_at) "
                "VALUES (%(project_id)s, %(display_name)s, "
                "%(first_seen_at)s, %(last_seen_at)s) "
                "ON CONFLICT (project_id) DO UPDATE SET "
                "  display_name = EXCLUDED.display_name, "
                "  first_seen_at = LEAST(projects.first_seen_at, "
                "                        EXCLUDED.first_seen_at), "
                "  last_seen_at = GREATEST(projects.last_seen_at, "
                "                          EXCLUDED.last_seen_at)",
                list(seen_projects.values()),
            )
            c.commit()


def _delete_orphans(seen_keys: set[str]) -> int:
    """Orphan files (R2 key gone) are deleted. CASCADE drops records."""
    with db.viz_conn() as c, c.cursor() as cur:
        if seen_keys:
            cur.execute(
                "DELETE FROM files WHERE file_key != ALL(%s) RETURNING 1",
                (list(seen_keys),),
            )
        else:
            cur.execute("DELETE FROM files RETURNING 1")
        deleted = len(cur.fetchall())
        c.commit()
    return deleted


class _Counts(NamedTuple):
    """What one run did. Zeros when the run died before doing anything.

    `skipped` is deliberately outside run_ingest's data-changed test:
    skipping the same foreign transcripts every hour changes nothing, and
    broadcasting ingest_done for it would make every idle run wake every
    connected dashboard.
    """
    listed: int = 0
    inserted: int = 0
    reparsed: int = 0
    deleted: int = 0
    skipped: int = 0

    @property
    def data_changed(self) -> bool:
        return bool(self.inserted or self.reparsed or self.deleted)


def _ingest_main(parser_version: str, failed: list[tuple[str, str]]
                 ) -> _Counts:
    """The whole R2->DB pass.

    Any exception propagates to run_ingest, which books it as the run's
    `fatal` and skips the post-passes.
    """
    existing = _existing_files()
    scan = _scan_r2()
    workers = _worker_count()
    project_paths = _resolve_project_paths(scan.marker_items, workers, failed)
    plan = _plan_work(scan.wire_objs, project_paths, existing, parser_version)
    inserted, reparsed, skipped = _fetch_and_persist(
        plan.todo, workers, parser_version, failed
    )
    _upsert_projects(plan.seen_projects)
    deleted = _delete_orphans(plan.seen_keys)
    # Both kinds of skip are the same fact — a transcript we saw and
    # deliberately did not ingest — so they share one counter: the ones
    # ruled out by key layout without a fetch, and the ones whose bytes
    # turned out to be a format we cannot parse yet.
    return _Counts(plan.listed, inserted, reparsed, deleted,
                   scan.foreign + skipped)


def _finish_run(run_id: int, started: datetime, trigger: str,
                counts: _Counts, fatal: str | None,
                failed: list[tuple[str, str]]) -> dict:
    """Book the run's outcome in ingest_runs and build the summary dict."""
    # `error` reports BOTH kinds of trouble, but only `fatal` gates anything.
    err = fatal if fatal is not None else _failure_summary(failed)
    finished = datetime.now(timezone.utc)
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ingest_runs SET finished_at=%s, r2_listed=%s, "
            "reparsed=%s, inserted=%s, deleted=%s, skipped=%s, error=%s "
            "WHERE id=%s",
            (finished, counts.listed, counts.reparsed, counts.inserted,
             counts.deleted, counts.skipped, err, run_id),
        )
        c.commit()
    return {
        "id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "trigger": trigger,
        "r2_listed": counts.listed,
        "inserted": counts.inserted,
        "reparsed": counts.reparsed,
        "deleted": counts.deleted,
        "skipped": counts.skipped,
        "failed": len(failed),
        "error": err,
    }


def run_ingest(trigger: str) -> dict:
    started = datetime.now(timezone.utc)
    parser_version = os.environ.get("PARSER_VERSION", "1")
    run_id = _begin_run(started, trigger)

    # Zeros when the run died early.
    counts = _Counts()
    # Per-object failures (key, message). Recorded in the run's `error`, but
    # deliberately NOT used to gate anything: one dropped connection out of
    # 1,464 files is a run with a retry pending, not a failed run.
    failed: list[tuple[str, str]] = []
    # A whole-run exception, which DOES gate the post-passes below.
    fatal = None

    try:
        counts = _ingest_main(parser_version, failed)
    except Exception as e:  # noqa: BLE001
        fatal = f"{type(e).__name__}: {e}"

    summary = _finish_run(run_id, started, trigger, counts, fatal, failed)

    # Gated on `fatal`, NOT on `err`: the derived state describes whatever
    # `records` now holds, so skipping the rebuild because one object out of
    # a thousand could not be fetched is what leaves the rollups and
    # is_canonical describing the PREVIOUS dataset until the next clean run.
    if fatal is None:
        # Order matters: the rollup reads is_canonical.
        recompute_canonical()
        rebuild_rollup()
        rebuild_tool_rollup()
        rebuild_latency_rollup()

    # Data changed: mark the response cache stale, then notify connected
    # SSE clients so the dashboard re-fetches without a page reload.
    #
    # invalidate() rather than clear(): clearing would drop every user onto
    # the uncached path on every ingest, so the refetch that ingest_done
    # triggers would block on a cold query. Marking stale keeps the entries
    # servable — the refetch returns the previous numbers instantly and the
    # fresh ones land via the background refresh. Threadsafe: ingest runs in
    # a scheduler thread.
    if fatal is None and counts.data_changed:
        cache.response_cache.invalidate()
        events.broadcast_threadsafe("ingest_done", summary)

    if fatal is None:
        warm_common()
    return summary


def _resolve(items: list, call, workers: int) -> list[tuple]:
    """Run `call(item)` over `items`, pairing each with its result OR its
    exception instead of letting the first failure escape.

    Sequential when workers == 1, on a pool otherwise. Both marker and wire
    fetches go through here: collecting with `[f.result() for f in
    as_completed(...)]` re-raised the worker's exception out of the
    collection step, which aborted the whole ingest AND discarded every
    already-fetched result alongside it. The two shapes have to behave
    identically, which is easiest to guarantee with one implementation.

    FatalFetchError is the one exception that still escapes: it means the
    fetch is broken rather than one object being unlucky, so it belongs to
    the run, not to the item.

    Returns [(item, result, None) | (item, None, exception)].
    """
    outcomes: list[tuple] = []
    if workers == 1:
        for item in items:
            try:
                outcomes.append((item, call(item), None))
            except FatalFetchError:
                raise
            except Exception as e:  # noqa: BLE001
                outcomes.append((item, None, e))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(call, item): item for item in items}
            for f in as_completed(futures):
                item = futures[f]
                try:
                    outcomes.append((item, f.result(), None))
                except FatalFetchError:
                    raise
                except Exception as e:  # noqa: BLE001
                    outcomes.append((item, None, e))
    return outcomes


def _record_failure(failed: list[tuple[str, str]], key: str,
                    exc: BaseException) -> None:
    """Book one object as failed and say so in the log."""
    failed.append((key, f"{type(exc).__name__}: {exc}"))
    log.warning(
        "ingest: %s failed after %d attempt(s): %s: %s",
        key, FETCH_ATTEMPTS, type(exc).__name__, exc,
    )


def _failure_summary(failed: list[tuple[str, str]]) -> str | None:
    """One line naming how many objects failed and which, or None.

    Goes into ingest_runs.error so a partial run is visible in the admin
    view, without pretending the whole run failed.
    """
    if not failed:
        return None
    keys = [key for key, _ in failed]
    shown = ", ".join(keys[:FAILURE_KEYS_IN_SUMMARY])
    if len(keys) > FAILURE_KEYS_IN_SUMMARY:
        shown += f", ... (+{len(keys) - FAILURE_KEYS_IN_SUMMARY} more)"
    noun = "object" if len(keys) == 1 else "objects"
    return f"{len(keys)} {noun} failed after retries: {shown}"


def rebuild_tool_rollup() -> int:
    """Rebuild `tool_rollup` from tool_uses + files.

    No join to `records`: tool_uses.line_num and records.line_num are
    disjoint in Kimi wire.jsonl, so there is nothing to match on and no
    per-tool-call model to record.

    n_total counts every tool call (/api/tool-usage), n_rated only the
    settled ones (/api/tool-error-rate's denominator), so one table serves
    both without either having to approximate the other. lines_added /
    lines_deleted are the parse-time edit churn sums behind
    /api/dashboard's Lines Added/Deleted panels.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        # DELETE, not TRUNCATE: TRUNCATE takes an ACCESS EXCLUSIVE lock for
        # the whole rebuild transaction, so every concurrent read of this
        # table blocks until it commits — measured at 2.9s for a SELECT that
        # normally takes 0.07s. The rebuild runs on every ingest, so that
        # stalled readers hourly. DELETE takes ROW EXCLUSIVE and MVCC keeps
        # serving the previous rows until commit, so readers never wait.
        c.execute("DELETE FROM tool_rollup")
        cur = c.execute(
            """
            INSERT INTO tool_rollup (
              hour, project_id, tool_name, n_total, n_rated, n_error,
              lines_added, lines_deleted
            )
            SELECT date_trunc('hour', tu.ts) AS hour,
                   f.project_id,
                   tu.tool_name,
                   COUNT(*)                                       AS n_total,
                   COUNT(*) FILTER (WHERE tu.is_error IS NOT NULL) AS n_rated,
                   COUNT(*) FILTER (WHERE tu.is_error)             AS n_error,
                   COALESCE(SUM(tu.lines_added), 0)               AS lines_added,
                   COALESCE(SUM(tu.lines_deleted), 0)             AS lines_deleted
              FROM tool_uses tu
              JOIN files f ON f.file_key = tu.file_key
             WHERE tu.ts IS NOT NULL
             GROUP BY 1, 2, 3
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_tool_rollup: %d rows", written)
    return written


# Display bucket widths /api/reply-latency can ask for, from
# api._bucket_seconds. The sub-hour widths (the 24h view) are deliberately
# absent: a row per 5 minutes of all history to serve one day is not worth
# it, and that range stays on the live path.
LATENCY_BUCKETS = (3600, 21600, 43200, 86400)

# Ranges warm_common pre-populates. Mirrors RangePicker's presets in
# src/app.jsx; anything the UI can request but this omits stays cold.
WARM_RANGES = ("all", "365d", "90d", "30d", "7d", "1d")


def rebuild_latency_rollup() -> int:
    """Rebuild `latency_rollup` for each display bucket width.

    Two passes per width — one grouped by project, one for the
    all-projects row (project_id = '') — because percentiles are not
    composable across a filter. Outlier dots are computed in the same
    pass and stored as JSONB.
    """
    written = 0
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '128MB'")
        # DELETE, not TRUNCATE: TRUNCATE takes an ACCESS EXCLUSIVE lock for
        # the whole rebuild transaction, so every concurrent read of this
        # table blocks until it commits — measured at 2.9s for a SELECT that
        # normally takes 0.07s. The rebuild runs on every ingest, so that
        # stalled readers hourly. DELETE takes ROW EXCLUSIVE and MVCC keeps
        # serving the previous rows until commit, so readers never wait.
        c.execute("DELETE FROM latency_rollup")
        for bs in LATENCY_BUCKETS:
            for scope_expr, scope_join in (
                ("f.project_id", "JOIN files f ON f.file_key = r.file_key"),
                ("''", ""),
            ):
                cur = c.execute(
                    db.sql_literal(f"""
                    WITH src AS (
                      SELECT to_timestamp(
                               floor(EXTRACT(EPOCH FROM r.ts) / {bs}) * {bs} + {bs} / 2
                             ) AS bucket,
                             {scope_expr} AS project_id,
                             COALESCE(NULLIF(r.model, ''), 'unknown') AS model,
                             r.ts, r.file_key, r.line_num,
                             r.reply_latency_s AS latency_s
                        FROM records r
                        {scope_join}
                       WHERE r.reply_latency_s IS NOT NULL
                    ),
                    bands AS (
                      SELECT bucket, project_id, model, COUNT(*) AS n,
                             PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY latency_s) AS p10,
                             PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_s) AS p50,
                             PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY latency_s) AS p90
                        FROM src GROUP BY 1, 2, 3
                    ),
                    ranked AS (
                      SELECT s.*,
                             b.n AS bucket_n,
                             ROW_NUMBER() OVER (PARTITION BY s.bucket, s.project_id, s.model
                                                ORDER BY s.latency_s DESC) AS rn_high,
                             ROW_NUMBER() OVER (PARTITION BY s.bucket, s.project_id, s.model
                                                ORDER BY s.latency_s ASC)  AS rn_low
                        FROM src s
                        JOIN bands b USING (bucket, project_id, model)
                       WHERE b.n >= 100
                    ),
                    picked AS (
                      SELECT bucket, project_id, model,
                             jsonb_agg(jsonb_build_object(
                               'ts', ts, 'latency_s', latency_s,
                               'file_key', file_key, 'line_num', line_num
                             ) ORDER BY latency_s DESC) AS outliers
                        FROM ranked
                       -- GREATEST(1, CEIL(n * 0.01)), matching the live
                       -- query exactly. `n / 100` would floor instead, so
                       -- a bucket of 150 would yield 1 dot, not 2.
                       WHERE rn_high <= GREATEST(1, CEIL(bucket_n * 0.01))
                          OR rn_low  <= GREATEST(1, CEIL(bucket_n * 0.01))
                       GROUP BY 1, 2, 3
                    )
                    INSERT INTO latency_rollup
                      (bucket_s, bucket, project_id, model, n, p10, p50, p90, outliers)
                    SELECT {bs}, b.bucket, b.project_id, b.model, b.n,
                           b.p10, b.p50, b.p90, COALESCE(p.outliers, '[]'::jsonb)
                      FROM bands b
                      LEFT JOIN picked p USING (bucket, project_id, model)
                    """)
                )
                written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_latency_rollup: %d rows", written)
    return written


def warm_common() -> None:
    """Pre-populate the response cache for the views a fresh visitor hits.

    After an ingest the buffer cache is cold (the recompute and rollup
    rebuild just rewrote the tables) and after a RESTART the response
    cache is empty too, so the first load pays both.
    Stale-while-revalidate cannot cover the restart case because there is
    nothing stale to serve.

    Only the unfiltered default views are warmed. The full keyspace is
    (endpoint x range x project x model), which is far too large to
    precompute and would mostly evict itself; a project the user actually
    opens still costs one cold query, but the landing view never does.

    Runs on the cache's background pool, so ingest returns immediately.
    Disabled by KIMIMETER_WARM_CACHE=0 — the tests set that, because a
    warm outlives the fixture that created its database and its queries
    then race the teardown that drops it.
    """
    if os.environ.get("KIMIMETER_WARM_CACHE", "1").lower() in ("0", "false", "no"):
        return

    # Every range the picker offers, so no button lands on a cold query.
    # Must mirror RangePicker's preset values in src/app.jsx — a range the
    # UI can request but this does not list is a permanently cold key.
    # ("1d" matters most: its 5-minute buckets are below the rollups' 1h
    # gate, so it is the only range still served by live queries.)
    for rng in WARM_RANGES:
        cache.warm(api_dashboard.dashboard, range_=rng)
        cache.warm(api.activity_heatmap, range_=rng)
        cache.warm(api.tool_usage, range_=rng)
        cache.warm(api.tool_error_rate, range_=rng)
        cache.warm(api.reply_latency, range_=rng)
        # /api/projects is range-scoped, so it needs warming per range like
        # everything else. Warming it bare took the endpoint's own
        # signature default ("30d") while the UI opens on "all", leaving
        # the one request every page load makes permanently uncached.
        cache.warm(api.list_projects, range_=rng)
    log.info("warm_common: queued %d range(s)", len(WARM_RANGES))


def recompute_canonical() -> int:
    """Resolve cross-file uuid dedup into `records.is_canonical`.

    Marks exactly the row that ``DISTINCT ON (r.uuid) ... ORDER BY r.uuid,
    r.file_key`` used to pick at read time, so the read endpoints can
    filter on a boolean instead of sorting the whole table on every
    request. ``line_num`` breaks ties within a file_key, which the old
    read-time ORDER BY left arbitrary.

    Rows with a NULL uuid are legacy records kept verbatim (they were the
    UNION ALL leg), so they are always canonical.

    Runs after EVERY successful ingest, not only when files changed: a
    freshly-migrated DB has the column defaulted to TRUE across the board,
    and skipping the pass on a no-op ingest would leave duplicates
    double-counted until something happened to change. The UPDATE only
    touches rows whose flag actually flips, so a steady-state pass writes
    nothing. Returns the number of rows changed.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        cur = c.execute(
            """
            UPDATE records r
               SET is_canonical = w.canon
              FROM (
                    SELECT file_key, line_num,
                           (uuid IS NULL OR ROW_NUMBER() OVER (
                              PARTITION BY uuid ORDER BY file_key, line_num
                            ) = 1) AS canon
                      FROM records
                   ) w
             WHERE r.file_key = w.file_key
               AND r.line_num = w.line_num
               AND r.is_canonical IS DISTINCT FROM w.canon
            """
        )
        changed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    if changed:
        log.info("recompute_canonical: %d rows reflagged", changed)
    return changed


def rebuild_rollup() -> int:
    """Rebuild `usage_rollup` from the canonical records.

    Full rebuild rather than an incremental merge: cross-file uuid dedup
    means a newly ingested FILE can demote a record that another file
    already contributed, so touched-files-only would leave stale sums
    behind. The whole table is ~1 row per (session, hour, model), which is
    a fraction of `records`, so rebuilding it costs one pass.

    Must run AFTER recompute_canonical() — it reads is_canonical.
    Returns the row count written.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        # DELETE, not TRUNCATE: TRUNCATE takes an ACCESS EXCLUSIVE lock for
        # the whole rebuild transaction, so every concurrent read of this
        # table blocks until it commits — measured at 2.9s for a SELECT that
        # normally takes 0.07s. The rebuild runs on every ingest, so that
        # stalled readers hourly. DELETE takes ROW EXCLUSIVE and MVCC keeps
        # serving the previous rows until commit, so readers never wait.
        c.execute("DELETE FROM usage_rollup")
        cur = c.execute(
            """
            INSERT INTO usage_rollup (
              session_id, project_id, hour, model, is_main,
              first_ts, last_ts, requests,
              fresh_tokens, output_tokens, cache_read_tokens, cost_usd
            )
            SELECT f.session_id,
                   f.project_id,
                   date_trunc('hour', r.ts)                    AS hour,
                   COALESCE(NULLIF(r.model, ''), 'unknown')    AS model,
                   f.is_main,
                   MIN(r.ts), MAX(r.ts), COUNT(*),
                   COALESCE(SUM(r.fresh_tokens), 0),
                   COALESCE(SUM(r.output_tokens), 0),
                   COALESCE(SUM(r.cache_read_tokens), 0),
                   COALESCE(SUM(r.cost_usd), 0)
              FROM records r
              JOIN files f ON f.file_key = r.file_key
             WHERE r.is_canonical AND r.ts IS NOT NULL
             GROUP BY 1, 2, 3, 4, 5
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_rollup: %d rows", written)
    return written


def _worker_count() -> int:
    """Fetch+parse concurrency.

    Unset or unparseable -> auto (network-bound work, so oversubscribe
    cores). An explicit number is honoured, clamped to at least 1, so
    INGEST_WORKERS=1 is a real "go sequential" switch for debugging.
    """
    auto = min(16, (os.cpu_count() or 4) * 2)
    raw = os.environ.get("INGEST_WORKERS", "").strip()
    if not raw:
        return auto
    try:
        return max(1, int(raw))
    except ValueError:
        return auto


def _fetch_with_retry(key: str) -> bytes:
    """One R2 GET, retried on transient failure. Runs on a pool thread.

    The retry lives here rather than in backend.r2 so the sidecar and
    stream readers keep their current single-shot semantics — only the
    ingest, which walks the whole bucket in one pass, needs to ride out a
    transient drop.

    Three outcomes, because "did that fail" is three questions, not two:

    - TRANSIENT_FETCH_ERRORS — retry, then propagate so the caller books
      one per-object failure.
    - CORRUPT_PAYLOAD_ERRORS — propagate on the FIRST attempt. Also one
      per-object failure, but no retry: the bytes will not improve.
    - anything else — a bug, re-raised as FatalFetchError so the
      per-object collector does not absorb it. A TypeError inside
      get_object would otherwise become a 1,464-object "partial run" that
      slept 37 minutes through the same bug instead of raising one loud
      traceback.
    """
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return r2.get_object(key)
        except CORRUPT_PAYLOAD_ERRORS:
            raise
        except TRANSIENT_FETCH_ERRORS:
            if attempt == FETCH_ATTEMPTS:
                raise
            log.warning(
                "ingest: fetch of %s failed (attempt %d/%d), retrying",
                key, attempt, FETCH_ATTEMPTS,
            )
            time.sleep(FETCH_BACKOFF_S[attempt - 1])
        except Exception as e:  # noqa: BLE001
            raise FatalFetchError(f"{key}: {type(e).__name__}: {e}") from e
    raise AssertionError("unreachable")  # pragma: no cover


def _fetch_and_parse(key: str) -> dict:
    """Runs on a pool thread. Touches no DB connection.

    Only the GET is retried: a parse failure is deterministic, so a second
    attempt reproduces the same error against the same bytes and buys
    nothing but delay.
    """
    return parse.parse_file(key, _fetch_with_retry(key))


def _fetch_marker(project_id: str, key: str) -> tuple[str, str] | None:
    """Fetch and parse a sessions/<hash>/project.json marker on a pool thread.

    Runs on a pool thread and touches no DB connection.

    The GET is retried and its failure is left to PROPAGATE, so the caller
    books it as a per-object failure like any wire object. Only decode and
    shape problems are swallowed here: a malformed marker means that
    project shows its id instead of its path, which is a degrade, not a
    failed fetch, and no retry would change it.
    """
    blob = _fetch_with_retry(key)
    try:
        data = json.loads(blob.decode("utf-8"))
        path = data.get("path")
        if isinstance(path, str) and path:
            return (project_id, path)
    except (ValueError, KeyError, AttributeError):
        # ValueError covers UnicodeDecodeError and json.JSONDecodeError;
        # AttributeError covers a marker whose top level is not an object.
        pass
    return None


def _persist(obj, proj, project_id, session_id, is_main, parsed,
             parser_version) -> None:
    """One file, one transaction — identical to the pre-pool behaviour."""
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) "
            "VALUES (%(project_id)s, %(display_name)s, "
            "%(first_seen_at)s, %(last_seen_at)s) "
            "ON CONFLICT (project_id) DO UPDATE SET "
            "  display_name = EXCLUDED.display_name, "
            "  first_seen_at = LEAST(projects.first_seen_at, "
            "                        EXCLUDED.first_seen_at), "
            "  last_seen_at = GREATEST(projects.last_seen_at, "
            "                          EXCLUDED.last_seen_at)",
            proj,
        )
        cur.execute(
            "DELETE FROM records WHERE file_key = %s", (obj.key,)
        )
        cur.execute(
            """
            INSERT INTO files (file_key, project_id, session_id,
              is_main, r2_etag, r2_size_bytes, r2_last_modified,
              parsed_at, parser_version, ctx_turns, turn_count,
              rate_limit_hits)
            VALUES (%(file_key)s, %(project_id)s, %(session_id)s,
              %(is_main)s, %(r2_etag)s, %(r2_size_bytes)s,
              %(r2_last_modified)s, %(parsed_at)s, %(parser_version)s,
              %(ctx_turns)s::jsonb, %(turn_count)s,
              %(rate_limit_hits)s::jsonb)
            ON CONFLICT (file_key) DO UPDATE SET
              project_id = EXCLUDED.project_id,
              session_id = EXCLUDED.session_id,
              is_main = EXCLUDED.is_main,
              r2_etag = EXCLUDED.r2_etag,
              r2_size_bytes = EXCLUDED.r2_size_bytes,
              r2_last_modified = EXCLUDED.r2_last_modified,
              parsed_at = EXCLUDED.parsed_at,
              parser_version = EXCLUDED.parser_version,
              ctx_turns = EXCLUDED.ctx_turns,
              turn_count = EXCLUDED.turn_count,
              rate_limit_hits = EXCLUDED.rate_limit_hits
            """,
            {
                "file_key": obj.key,
                "project_id": project_id,
                "session_id": session_id,
                "is_main": is_main,
                "r2_etag": obj.etag,
                "r2_size_bytes": obj.size,
                "r2_last_modified": obj.last_modified,
                "parsed_at": datetime.now(timezone.utc),
                "parser_version": parser_version,
                "ctx_turns": json.dumps(parsed["ctx_turns"], default=str),
                "turn_count": parsed["turn_count"],
                "rate_limit_hits": json.dumps(
                    parsed.get("rate_limit_hits", []), default=str
                ),
            },
        )
        cur.execute(
            "DELETE FROM tool_uses WHERE file_key = %s", (obj.key,)
        )
        if parsed.get("tool_uses"):
            cur.executemany(
                """
                INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name,
                  is_error, lines_added, lines_deleted)
                VALUES (%(file_key)s, %(line_num)s, %(idx)s, %(ts)s, %(tool_name)s,
                  %(is_error)s, %(lines_added)s, %(lines_deleted)s)
                """,
                parsed["tool_uses"],
            )
        if parsed["records"]:
            cur.executemany(
                """
                INSERT INTO records (file_key, line_num, uuid,
                  ts, model, fresh_tokens,
                  cache_creation_tokens, cache_read_tokens,
                  output_tokens, cost_usd,
                  text_chars, reply_latency_s)
                VALUES (%(file_key)s, %(line_num)s, %(uuid)s,
                  %(ts)s, %(model)s,
                  %(fresh_tokens)s, %(cache_creation_tokens)s,
                  %(cache_read_tokens)s, %(output_tokens)s,
                  %(cost_usd)s,
                  %(text_chars)s, %(reply_latency_s)s)
                """,
                parsed["records"],
            )
        c.commit()

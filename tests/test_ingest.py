import json
import lzma
import os
import shutil
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from botocore.exceptions import ConnectionClosedError

from backend import api, api_dashboard, cache, db, ingest
from backend.cache import _warm_kwargs
from backend.ingest import _failure_summary
from tests.conftest import run_db_name

_REPO_ROOT = Path(__file__).resolve().parent.parent

# One of the five wire.jsonl keys in fixtures/r2_mini, used as the object
# whose fetch is made to fail.
_FLAKY_KEY = "sessions/projA/sess-A/wire.jsonl"


@pytest.fixture(name="fresh_db")
def _fresh_db(monkeypatch):
    """Per-test schema reset on a separate DB."""
    test_db = run_db_name("kimimeter_test")
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    db.close_viz_pool()
    yield
    db.close_viz_pool()
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


@pytest.fixture(name="mini_r2_env")
def _mini_r2_env(monkeypatch):
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="kd-ingest-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
    yield Path(tmp) / "r2" / "kimi"
    shutil.rmtree(tmp)


def _scalar(c, sql: str, args: tuple = ()):
    """First column of the first row of a query that must return one.

    COUNT/MAX-style aggregates always yield a row; fetchone() is typed
    Optional, so a None here means the test's premise broke — assert
    instead of subscripting it.
    """
    # args defaults to "no params at all", NOT (): several queries carry a
    # literal LIKE '%...' that psycopg would read as a placeholder the
    # moment a params sequence is passed.
    row = (c.execute(sql, args) if args else c.execute(sql)).fetchone()
    assert row is not None, f"query returned no rows: {sql[:60]}"
    return row[0]


def _wire_blob(message_id, input_other, output):
    return (
        b'{"timestamp":"2026-06-14T12:00:00Z","message":{"type":"StatusUpdate",'
        b'"payload":{"message_id":"%b","token_usage":{'
        b'"input_other":%b,"input_cache_creation":0,"input_cache_read":0,"output":%b}}}}\n'
        % (message_id.encode(), str(input_other).encode(), str(output).encode())
    )


def test_ingest_inserts_one_row_per_jsonl(fresh_db, mini_r2_env):
    """Mini mirror has 5 wire.jsonls (4 main + 1 subagent peer) under 4
    sessions in 2 projects. Expect 5 rows in `files`, 4 with is_main=true,
    4 distinct session_ids, 2 projects."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 5
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM files")
        assert n == 5
        n_main = _scalar(c, "SELECT COUNT(*) FROM files WHERE is_main")
        assert n_main == 4
        n_sess = _scalar(c, "SELECT COUNT(DISTINCT session_id) FROM files")
        assert n_sess == 4
        n_proj = _scalar(c, "SELECT COUNT(*) FROM projects")
        assert n_proj == 2


def test_records_populated_with_no_write_time_dedup(fresh_db, mini_r2_env):
    """sess-C main + sess-C subagent + sess-D main all have uuid='shared-uuid-1'.
    The ingest writes per-file with NO cross-file dedup at write time
    — so records has ALL three rows. Query-time DISTINCT ON is the dedup."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM records")
        assert n > 0
        cnt = _scalar(
            c, "SELECT COUNT(*) FROM records WHERE uuid = 'shared-uuid-1'"
        )
        assert cnt == 3
        cnt_distinct = _scalar(
            c, "SELECT COUNT(DISTINCT uuid) FROM records WHERE uuid = 'shared-uuid-1'"
        )
        assert cnt_distinct == 1


def test_ctx_turns_stored_per_file(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT file_key, turn_count, jsonb_array_length(ctx_turns) FROM files"
        ).fetchall()
    for fk, tc, jlen in rows:
        assert tc == jlen, f"{fk}: turn_count={tc} but ctx_turns has {jlen}"


def test_etag_change_triggers_per_file_reparse(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before_etag = _scalar(
            c, "SELECT r2_etag FROM files WHERE file_key LIKE '%sess-A/wire.jsonl'"
        )
    target = mini_r2_env / "sessions" / "projA" / "sess-A" / "wire.jsonl"
    target.write_text(target.read_text() + "\n")
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 1
    with db.viz_conn() as c:
        after_etag = _scalar(
            c, "SELECT r2_etag FROM files WHERE file_key LIKE '%sess-A/wire.jsonl'"
        )
    assert before_etag != after_etag


def test_parser_version_bump_reparses_all(fresh_db, mini_r2_env, monkeypatch):
    ingest.run_ingest(trigger="manual")
    monkeypatch.setenv("PARSER_VERSION", "2")
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 5


def test_deleted_file_removed(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    target = mini_r2_env / "sessions" / "projA" / "sess-B" / "wire.jsonl"
    target.unlink()
    result = ingest.run_ingest(trigger="manual")
    assert result["deleted"] == 1
    with db.viz_conn() as c:
        n = _scalar(
            c, "SELECT COUNT(*) FROM files WHERE file_key LIKE '%sess-B/wire.jsonl'"
        )
        assert n == 0


def test_records_cascade_on_file_delete(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    target = mini_r2_env / "sessions" / "projA" / "sess-A" / "wire.jsonl"
    target.unlink()
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        n = _scalar(
            c, "SELECT COUNT(*) FROM records WHERE file_key LIKE '%sess-A/wire.jsonl'"
        )
        assert n == 0


def test_no_changes_second_run_is_zero_reparse(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    result2 = ingest.run_ingest(trigger="manual")
    assert result2["inserted"] == 0
    assert result2["reparsed"] == 0


def test_edit_churn_persisted_and_rolled_up(fresh_db, mini_r2_env):
    """End to end: a wire.jsonl whose Edit/Write calls carry known churn
    lands in tool_uses, and rebuild_tool_rollup sums it. The fixture is
    the parser's own line_churn_kimi_code.jsonl: Edit 4+/3-, Write 2+/0-,
    plus an errored Edit that must contribute nothing."""
    target_dir = mini_r2_env / "sessions" / "projA" / "sess-churn"
    target_dir.mkdir(parents=True)
    shutil.copy(
        _REPO_ROOT / "fixtures/parser/line_churn_kimi_code.jsonl",
        target_dir / "wire.jsonl",
    )
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT tool_name, is_error, lines_added, lines_deleted "
            "FROM tool_uses WHERE file_key LIKE '%sess-churn/wire.jsonl' "
            "ORDER BY line_num"
        ).fetchall()
        assert rows == [
            ("Edit", False, 4, 3),
            ("Write", False, 2, 0),
            ("Edit", True, 0, 0),
            ("Bash", False, 0, 0),
        ]
        added = _scalar(
            c, "SELECT COALESCE(SUM(lines_added), 0) FROM tool_rollup"
        )
        deleted = _scalar(
            c, "SELECT COALESCE(SUM(lines_deleted), 0) FROM tool_rollup"
        )
        assert (added, deleted) == (6, 3)


def test_is_canonical_matches_read_time_distinct_on(fresh_db, mini_r2_env):
    """The ingest-time flag must select exactly the rows the old read-time
    `DISTINCT ON (uuid) ORDER BY uuid, file_key` would have kept.

    This is the invariant that lets the read endpoints filter a boolean
    instead of re-sorting the whole table (SV-CANONICAL-FLAG). The mini
    mirror carries a cross-session shared uuid, so there is a real
    duplicate to resolve.
    """
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        flagged = c.execute(
            "SELECT file_key, line_num FROM records "
            "WHERE is_canonical ORDER BY file_key, line_num"
        ).fetchall()
        # What the read endpoints used to compute on every request.
        expected = c.execute(
            """
            SELECT file_key, line_num FROM (
              (SELECT DISTINCT ON (uuid) file_key, line_num
                 FROM records WHERE uuid IS NOT NULL
                ORDER BY uuid, file_key, line_num)
              UNION ALL
              (SELECT file_key, line_num FROM records WHERE uuid IS NULL)
            ) t ORDER BY file_key, line_num
            """
        ).fetchall()
        dupes = c.execute(
            "SELECT COUNT(*) FROM records WHERE NOT is_canonical"
        ).fetchone()

    assert flagged == expected
    assert dupes is not None and dupes[0] > 0, (
        "fixture must contain a cross-file duplicate, or this proves nothing"
    )


def test_recompute_canonical_is_idempotent(fresh_db, mini_r2_env):
    """A steady-state pass must not rewrite rows — it runs after every
    ingest, including no-op ones."""
    ingest.run_ingest(trigger="manual")
    assert ingest.recompute_canonical() == 0


def test_usage_rollup_matches_the_records_aggregate(fresh_db, mini_r2_env):
    """The rollup is derived state serving the dashboard's totals, so it
    must sum to exactly what the canonical records do. Anything else is a
    silently wrong number on screen."""
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        rolled = c.execute(
            "SELECT SUM(requests), SUM(fresh_tokens), SUM(output_tokens), "
            "       SUM(cache_read_tokens), ROUND(SUM(cost_usd), 6), "
            "       COUNT(DISTINCT session_id) "
            "FROM usage_rollup"
        ).fetchone()
        raw = c.execute(
            "SELECT COUNT(*), SUM(r.fresh_tokens), SUM(r.output_tokens), "
            "       SUM(r.cache_read_tokens), ROUND(SUM(r.cost_usd), 6), "
            "       COUNT(DISTINCT f.session_id) "
            "FROM records r JOIN files f ON f.file_key = r.file_key "
            "WHERE r.is_canonical AND r.ts IS NOT NULL"
        ).fetchone()

    assert rolled == raw
    assert raw is not None and raw[0] > 0, "fixture produced no records"


def test_rebuild_rollup_is_a_full_replace(fresh_db, mini_r2_env):
    """A rebuild must not accumulate: it TRUNCATEs first, because cross-file
    dedup can demote a record another file already contributed."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = c.execute("SELECT COUNT(*) FROM usage_rollup").fetchone()
    ingest.rebuild_rollup()
    with db.viz_conn() as c:
        after = c.execute("SELECT COUNT(*) FROM usage_rollup").fetchone()
    assert before == after


def test_warm_common_covers_every_warmed_range(fresh_db, mini_r2_env, monkeypatch):
    """Every endpoint warm_common touches must be warmed for EVERY range
    it claims to cover — a warm keyed on something the UI never requests
    is dead work and leaves the real key cold.

    Regression: /api/projects gained a `range` parameter, but warm_common
    still called cache.warm(api.list_projects) bare. cache.warm falls back
    to the endpoint's signature default ("30d") while the UI opens on
    "all", so the one request every page load makes was never warmed.
    """
    monkeypatch.setenv("KIMIMETER_WARM_CACHE", "1")
    ingest.run_ingest(trigger="manual")

    warmed = (
        api_dashboard.dashboard, api.activity_heatmap, api.tool_usage,
        api.tool_error_rate, api.reply_latency, api.list_projects,
    )
    # The warms run on a background pool; give them a bounded moment.
    deadline = time.time() + 60
    missing = None
    while time.time() < deadline:
        missing = [
            f"{fn.__qualname__}(range={rng})"
            for rng in ingest.WARM_RANGES
            for fn in warmed
            if cache.response_cache.get(_warm_key(fn, rng)) is None
        ]
        if not missing:
            break
        time.sleep(0.25)

    assert not missing, "warm_common left these uncached: " + ", ".join(missing)


def _warm_key(fn, rng: str) -> str:
    """Reproduce cache_response's key for a request at `rng`.

    The kwargs come from backend/cache.py's _warm_kwargs — the very
    derivation warm() uses — so renaming an endpoint parameter cannot
    desync what this probes from what warm() writes. Only the final key
    formatting is reproduced here (issue #18).
    """
    kwargs = _warm_kwargs(fn, range_=rng)
    target = getattr(fn, "__wrapped__", fn)
    return target.__qualname__ + ":" + repr(sorted(kwargs.items()))


def test_ingest_marks_response_cache_stale(fresh_db, mini_r2_env):
    """Ingest marks cached responses stale but leaves them SERVABLE.

    Clearing outright would drop every reader onto the uncached path on
    each ingest. Stale-while-revalidate keeps the previous numbers
    available while the refresh runs off the request path.
    """
    cache.response_cache.put("stale-key", {"v": "old"})
    entry = cache.response_cache.get_entry("stale-key")
    assert entry == ({"v": "old"}, False), "fresh before ingest"

    ingest.run_ingest(trigger="manual")

    entry = cache.response_cache.get_entry("stale-key")
    assert entry is not None, "ingest must NOT drop the entry"
    value, is_stale = entry
    assert value == {"v": "old"}, "previous response still servable"
    assert is_stale is True, "and flagged for background refresh"


def test_first_seen_at_uses_least(fresh_db, mini_r2_env):
    """projects.first_seen_at must NOT be locked at first-ingest mtime.
    Add a NEW file under an existing project with an earlier mtime;
    re-ingest must drag first_seen_at backward via LEAST(...) in ON CONFLICT."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = _scalar(
            c, "SELECT first_seen_at FROM projects WHERE project_id = 'projA'"
        )

    new_dir = mini_r2_env / "sessions" / "projA" / "sess-NEW"
    new_dir.mkdir()
    new_file = new_dir / "wire.jsonl"
    new_file.write_bytes(_wire_blob("u-new", 1, 1))
    older_ts = before.timestamp() - 3600
    os.utime(new_file, (older_ts, older_ts))

    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        after = _scalar(
            c, "SELECT first_seen_at FROM projects WHERE project_id = 'projA'"
        )
    assert after < before, f"first_seen_at should move backward: was {before}, now {after}"


def test_pool_and_sequential_ingest_agree(fresh_db, mini_r2_env, monkeypatch):
    """Fetch+parse on a thread pool and sequentially must produce identical
    persistence."""

    def _counts_after_ingest(workers):
        monkeypatch.setenv("INGEST_WORKERS", str(workers))
        result = ingest.run_ingest(trigger="manual")
        assert result["error"] is None
        with db.viz_conn() as c:
            files = _scalar(c, "SELECT COUNT(*) FROM files")
            records = _scalar(c, "SELECT COUNT(*) FROM records")
        return files, records

    seq_files, seq_records = _counts_after_ingest(1)

    # Reset the DB for an independent parallel run.
    test_db = os.environ["DATABASE_URL_VIZ"].replace("postgresql:///", "")
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    db.close_viz_pool()

    par_files, par_records = _counts_after_ingest(4)
    assert seq_files == par_files
    assert seq_records == par_records


# ------------------------------------------- per-object R2 failures (#3)

def _patch_fetch(monkeypatch, key, fail_times, exc=None):
    """Make r2.get_object fail for `key` on its first `fail_times` calls.

    `exc` is the exception to raise, defaulting to a plain OSError.
    Returns (call_counts, slept) — the per-key GET count (the pooled path
    calls this from several threads, hence the lock) and the backoff sleeps
    the retry asked for, which are swallowed so the suite does not pay them.
    """
    real_get = ingest.r2.get_object
    counts: Counter = Counter()
    slept: list[float] = []
    lock = threading.Lock()

    def flaky(k):
        with lock:
            counts[k] += 1
            n = counts[k]
        if k == key and n <= fail_times:
            raise exc or OSError(f"connection reset while fetching {k}")
        return real_get(k)

    monkeypatch.setattr(ingest.r2, "get_object", flaky)
    monkeypatch.setattr(ingest.time, "sleep", slept.append)
    return counts, slept


@pytest.mark.parametrize("workers", [1, 4])
def test_one_failed_object_does_not_abort_the_run(
    fresh_db, mini_r2_env, monkeypatch, workers
):
    """A single unfetchable object costs that object, not the ingest.

    Both the sequential and the pooled path are exercised: they collect
    results differently (a generator consumed in the persist loop vs
    as_completed), and each used to let the exception escape.
    """
    monkeypatch.setenv("INGEST_WORKERS", str(workers))
    counts, _ = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=99)

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 1
    assert result["inserted"] == 4, "the other four files must still persist"
    assert result["error"] == (
        f"1 object failed after retries: {_FLAKY_KEY}"
    )
    assert counts[_FLAKY_KEY] == ingest.FETCH_ATTEMPTS
    with db.viz_conn() as c:
        keys = [r[0] for r in c.execute(
            "SELECT file_key FROM files ORDER BY file_key"
        ).fetchall()]
    assert _FLAKY_KEY not in keys
    assert len(keys) == 4


def test_per_object_failure_still_rebuilds_derived_state(
    fresh_db, mini_r2_env, monkeypatch
):
    """The regression that matters: derived state must not be left stale.

    recompute_canonical() and the rollups describe whatever `records` now
    holds. Gating them on a flawless run meant one dropped connection left
    `usage_rollup` / `tool_rollup` describing the PREVIOUS dataset and
    is_canonical un-recomputed until some later run happened to be clean.
    """
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rollup_before = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
        dupes_before = _scalar(c, "SELECT COUNT(*) FROM records WHERE NOT is_canonical")
    assert rollup_before > 0 and dupes_before > 0, "fixture proves nothing"

    with db.viz_conn() as c:
        # Tool calls hang off the file whose fetch fails, so the reparse
        # never deletes them and tool_rollup has something to rebuild from.
        c.execute(
            "INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, "
            "is_error) VALUES (%s, 9001, 0, now(), 'Read', false)",
            (_FLAKY_KEY,),
        )
        # Wreck every piece of derived state, then prove the run restores it.
        c.execute("TRUNCATE usage_rollup")
        c.execute("TRUNCATE tool_rollup")
        c.execute("UPDATE records SET is_canonical = TRUE")
        c.commit()

    monkeypatch.setenv("PARSER_VERSION", "2")
    _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=99)
    result = ingest.run_ingest(trigger="manual")
    assert result["failed"] == 1
    assert result["reparsed"] == 4

    with db.viz_conn() as c:
        rollup_after = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
        dupes_after = _scalar(c, "SELECT COUNT(*) FROM records WHERE NOT is_canonical")
        tool_rollup_after = _scalar(c, "SELECT COUNT(*) FROM tool_rollup")
    assert rollup_after == rollup_before, "usage_rollup was not rebuilt"
    assert dupes_after == dupes_before, "is_canonical was not recomputed"
    assert tool_rollup_after > 0, "tool_rollup was not rebuilt"


def test_a_transient_fetch_failure_is_retried_and_recovers(
    fresh_db, mini_r2_env, monkeypatch
):
    """Two failed GETs then a good one: the file lands, the run is clean."""
    counts, slept = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=2)

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 0
    assert result["error"] is None
    assert result["inserted"] == 5
    assert counts[_FLAKY_KEY] == 3
    assert slept == [0.5, 1.0], "exponential backoff between attempts"
    with db.viz_conn() as c:
        n = _scalar(
            c, "SELECT COUNT(*) FROM files WHERE file_key = %s", (_FLAKY_KEY,)
        )
    assert n == 1


def test_fetch_gives_up_after_three_attempts(
    fresh_db, mini_r2_env, monkeypatch
):
    """The retry is bounded — it must not spin on a genuinely dead object."""
    counts, slept = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=99)

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 3
    assert slept == [0.5, 1.0]
    assert result["failed"] == 1
    assert "connection reset" not in (result["error"] or ""), \
        "the summary names keys, not stack noise"
    assert _FLAKY_KEY in result["error"]


_CORRUPT_XZ_KEY = "sessions/projC/sess-E/wire.jsonl.xz"


def test_a_corrupt_xz_object_is_one_failure_not_a_dead_run(
    fresh_db, mini_r2_env, monkeypatch
):
    """Real invalid bytes under a `.xz` key, not a monkeypatched raise.

    r2.get_object inflates `.xz` transparently, so lzma raises from inside
    the fetch — and lzma.LZMAError is not an OSError. Every production
    object is `.xz`, so classifying it as "not transient, therefore a bug"
    would abort the entire run over one truncated upload: issue #3's
    original failure mode, for 100% of the bucket.
    """
    corrupt = mini_r2_env / "sessions" / "projC" / "sess-E" / "wire.jsonl.xz"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"\xfd7zXZ\x00 this is not a valid xz stream \x00\x01")
    with pytest.raises(lzma.LZMAError):
        lzma.decompress(corrupt.read_bytes())      # the fixture must really be corrupt

    counts, slept = _patch_fetch(monkeypatch, _CORRUPT_XZ_KEY, fail_times=0)

    result = ingest.run_ingest(trigger="manual")

    assert result["r2_listed"] == 6
    assert result["failed"] == 1
    assert result["error"] == (
        f"1 object failed after retries: {_CORRUPT_XZ_KEY}"
    )
    assert result["inserted"] == 5, "the intact objects are still persisted"
    assert counts[_CORRUPT_XZ_KEY] == 1, "a corrupt object must not be re-fetched"
    assert not slept, "and must not sleep between attempts it does not make"
    with db.viz_conn() as c:
        rollup = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
        stored = _scalar(
            c, "SELECT COUNT(*) FROM files WHERE file_key = %s", (_CORRUPT_XZ_KEY,)
        )
    assert rollup > 0, "derived state must still be rebuilt"
    assert stored == 0


def test_a_programming_error_in_the_fetch_is_not_retried(
    fresh_db, mini_r2_env, monkeypatch
):
    """A bug is not a transient, and must not be dressed up as one.

    Retrying a TypeError sleeps 1.5s per object and books it as a
    per-object failure — at 1,464 objects that is a silent 37-minute
    "partial run" instead of one loud traceback.
    """
    counts, slept = _patch_fetch(
        monkeypatch, _FLAKY_KEY, fail_times=99,
        exc=TypeError("get_object() takes 1 positional argument but 2 were given"),
    )

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 1, "a bug must not be retried"
    assert not slept, "and must not sleep"
    assert result["failed"] == 0, "it is not a per-object failure"
    assert result["error"].startswith("FatalFetchError:"), result["error"]
    assert "TypeError" in result["error"], "the type must survive into the run"
    with db.viz_conn() as c:
        rollup = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
    assert rollup == 0, "a fatal run must not rebuild derived state"


def test_a_parse_failure_is_not_retried(fresh_db, mini_r2_env, monkeypatch):
    """Parsing is deterministic: re-fetching the same bytes buys nothing."""
    real_get = ingest.r2.get_object
    counts: Counter = Counter()
    lock = threading.Lock()

    def counting(k):
        with lock:
            counts[k] += 1
        return real_get(k)

    real_parse = ingest.parse.parse_file

    def boom(file_key, blob):
        if file_key == _FLAKY_KEY:
            raise ValueError("malformed line 3")
        return real_parse(file_key, blob)

    monkeypatch.setattr(ingest.r2, "get_object", counting)
    monkeypatch.setattr(ingest.parse, "parse_file", boom)
    monkeypatch.setattr(ingest.time, "sleep", lambda s: pytest.fail(
        "a parse failure must not sleep on a retry"
    ))

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 1, "the GET must not be repeated"
    assert result["failed"] == 1
    assert result["inserted"] == 4
    assert "ValueError" not in (result["error"] or "")


_MARKER_KEY = "sessions/projA/project.json"


def _write_marker(mini_r2_env, path="/root/work/projA"):
    """The mini mirror carries no project.json; production has 228 of them."""
    marker = mini_r2_env / "sessions" / "projA" / "project.json"
    marker.write_text(json.dumps({"path": path}))
    return marker


@pytest.mark.parametrize("workers", [1, 4])
def test_a_failed_marker_fetch_does_not_abort_the_run(
    fresh_db, mini_r2_env, monkeypatch, workers
):
    """Marker GETs are as droppable as wire GETs, and fail earlier.

    The boto3 transients are NOT OSErrors — ConnectionClosedError, the one
    issue #3 was filed about, is a BotoCoreError — so _fetch_marker's narrow
    catch never saw them and the exception escaped the collection step
    before r2_listed had even been counted.
    """
    _write_marker(mini_r2_env)
    monkeypatch.setenv("INGEST_WORKERS", str(workers))
    boom = ConnectionClosedError(endpoint_url="https://acct.r2.cloudflarestorage.com")
    assert not isinstance(boom, OSError), "the point of the fixture"
    counts = _patch_fetch(monkeypatch, _MARKER_KEY, fail_times=99, exc=boom)[0]

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 1
    assert result["error"] == f"1 object failed after retries: {_MARKER_KEY}"
    assert result["r2_listed"] == 5, "the wire objects were still listed"
    assert result["inserted"] == 5, "and still persisted"
    assert counts[_MARKER_KEY] == ingest.FETCH_ATTEMPTS, "the GET is retried"
    with db.viz_conn() as c:
        rollup = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = 'projA'"
        )
    assert rollup > 0, "derived state must still be rebuilt"
    assert display == "projA", "an unreadable marker degrades to the id"


def test_a_malformed_marker_degrades_without_counting_as_a_failure(
    fresh_db, mini_r2_env
):
    """Decode/shape problems are not fetch failures: no retry would help,
    and the project simply shows its id instead of its path."""
    marker = _write_marker(mini_r2_env)
    marker.write_text("{not json at all")

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 0
    assert result["error"] is None
    with db.viz_conn() as c:
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = 'projA'"
        )
    assert display == "projA"


def test_a_readable_marker_still_names_the_project(fresh_db, mini_r2_env):
    """Guard the happy path the retry/collect rework runs through."""
    _write_marker(mini_r2_env, path="/root/work/projA")

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 0
    with db.viz_conn() as c:
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = 'projA'"
        )
    assert display == "/root/work/projA"


def test_failure_summary_truncates_a_long_key_list():
    """A 1,464-file run losing its connection must not write a novel into
    ingest_runs.error."""
    failed = [(f"sessions/p/s{i}/wire.jsonl", "OSError: boom") for i in range(9)]
    summary = _failure_summary(failed)
    assert summary is not None
    assert summary.startswith("9 objects failed after retries: ")
    assert summary.endswith(", ... (+4 more)")
    assert summary.count("wire.jsonl") == ingest.FAILURE_KEYS_IN_SUMMARY
    assert _failure_summary([]) is None

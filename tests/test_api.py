import json
import os
import shutil
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend import api as api_mod
from backend import api_dashboard as api_dash_mod
from backend import api_sessions as api_sess_mod
from backend import cache, db, ingest, pricing
from backend.api import _tool_source

_REPO_ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def _pg_cur():
    """Cursor over a direct psycopg connection to the scratch DB.

    Deliberately NOT `with psycopg.connect(...) as conn`: pylint infers
    the return of Connection.connect as None and reports
    not-context-manager, a false positive.

    `contextlib.closing` rather than a bare assignment plus try/finally,
    which is what this used to be. pylint 4.0.7 against psycopg 3.3
    infers a bare `conn = psycopg.connect(...)` as `Class 'value'` and
    then reports no-member on every .cursor()/.commit()/.close() — and
    annotating the variable does not help, because it cannot resolve
    `psycopg.Connection` either. Routing through closing() sidesteps both
    false positives without disabling no-member, which would switch the
    check off for genuine mistakes in here too. It is also what the
    sibling repo already does.

    Semantics are unchanged: commit on success, close always. closing()
    only closes, so the commit stays explicit.
    """
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn:
        with closing(conn.cursor()) as cur:
            yield cur
        conn.commit()


def _build_app(monkeypatch, pre_ingest=None, test_db="kimimeter_test_api"):
    """Spin up a fresh DB + mini R2, optionally mutate the temp R2 tree,
    ingest, and yield a TestClient on the api router — then tear it down.

    Bypasses auth via a clean FastAPI app with only the api router.
    """
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="kd-api-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")

    if pre_ingest is not None:
        pre_ingest(Path(tmp) / "r2")

    db.close_viz_pool()

    ingest.run_ingest(trigger="manual")

    a = FastAPI()
    a.include_router(api_mod.router)
    a.include_router(api_dash_mod.router)
    a.include_router(api_sess_mod.router)

    yield TestClient(a)

    db.close_viz_pool()
    shutil.rmtree(tmp)
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


# Module-scoped: this setup (dropdb, createdb, schema, copy the R2 tree,
# a full ingest incl. recompute_canonical + rebuild_rollup) ran per test
# and was seconds of pure `setup` on every one of ~40 read-only tests —
# the bulk of the suite's runtime. Tests that WRITE must not share it;
# they take `app_with_fresh_data` below.
@pytest.fixture(scope="module", name="app_with_data")
def _app_with_data():
    mp = pytest.MonkeyPatch()          # monkeypatch itself is function-scoped
    try:
        yield from _build_app(mp)
    finally:
        mp.undo()


@pytest.fixture(name="app_with_fresh_data")
def _app_with_fresh_data():
    """Function-scoped variant for tests that MUTATE rows, so they cannot
    contaminate the shared module-scoped client."""
    mp = pytest.MonkeyPatch()
    try:
        yield from _build_app(mp, test_db="kimimeter_test_api_mut")
    finally:
        mp.undo()


@pytest.fixture(name="app_with_unresolved")
def _app_with_unresolved():
    """Plain fixture plus two junk hash-projects (no project.json marker).

    Function-scoped ON PURPOSE, unlike `app_with_data`. Both fixtures point
    the process-global DATABASE_URL_VIZ at their own database, so a
    module-scoped one here would build once, leave the env pointing at the
    junk DB, and every later `app_with_data` test would read it — the
    cached module fixture never re-points anything. Function scope means
    the MonkeyPatch undo restores the shared DSN after each test.

    One 32-hex legacy md5 id and one 12-hex kimi-code workdir hash id,
    each with a distinct session dir so session_count sees 2.
    """
    def _inject_junk(r2_root: Path):
        src_wire = (
            _REPO_ROOT
            / "fixtures/r2_mini/kimi/sessions/projB/sess-C/subagents/agent-aaaa/wire.jsonl"
        )
        template = src_wire.read_text()
        junk = [
            ("0123456789abcdef0123456789abcdef", "test1", "unresolved-uuid-32"),
            ("abcdef012345", "test2", "unresolved-uuid-12"),
        ]
        for project_id, session_id, uuid in junk:
            wire = template.replace('"shared-uuid-1"', f'"{uuid}"')
            dest = (
                r2_root
                / "kimi/sessions"
                / project_id
                / session_id
                / "subagents/agent-x/wire.jsonl"
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(wire)

    mp = pytest.MonkeyPatch()
    try:
        yield from _build_app(
            mp, pre_ingest=_inject_junk, test_db="kimimeter_test_unres"
        )
    finally:
        mp.undo()


@pytest.fixture(name="app_with_two_models")
def _app_with_two_models():
    """Plain fixture plus one extra session dated before
    parse.MODEL_CUTOFF_DT, so it resolves to "kimi-k2-6" instead of the
    "kimi-k2-7-code" every other fixture session gets. All fixture data
    is a canonical, exact-rate label — this is the only way to get a
    genuinely second model into a /api/cache response without patching
    pricing.MODEL_RATES underneath the ingest itself.

    Function-scoped, like app_with_unresolved, for the same reason: it
    points DATABASE_URL_VIZ at its own DB.
    """
    def _inject_early_model(r2_root: Path):
        early = r2_root / "kimi/sessions/projA/sess-E/wire.jsonl"
        early.parent.mkdir(parents=True, exist_ok=True)
        early.write_text(json.dumps({
            "timestamp": "2026-06-01T00:00:00Z",
            "message": {
                "type": "StatusUpdate",
                "payload": {
                    "message_id": "msg-sess-e",
                    "token_usage": {
                        "input_other": 40,
                        "input_cache_creation": 0,
                        "input_cache_read": 0,
                        "output": 20,
                    },
                },
            },
        }) + "\n")

    mp = pytest.MonkeyPatch()
    try:
        yield from _build_app(
            mp, pre_ingest=_inject_early_model, test_db="kimimeter_test_twomodel"
        )
    finally:
        mp.undo()


def test_projects(app_with_data):
    r = app_with_data.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    pids = sorted(p["project_id"] for p in body["projects"])
    assert pids == ["projA", "projB"]
    for p in body["projects"]:
        assert "session_count" in p and "total_cost" in p


def test_projects_range_scoped_ordering_and_zero_cost_exclusion(app_with_fresh_data):
    """SV-ISSUE-7: /api/projects orders by RANGE-scoped cost and drops any
    project whose ALL-TIME cost is 0 — two different aggregates that must
    not be conflated. A project with real all-time cost but nothing in the
    selected range stays listed, sorted last, with its reported (range)
    cost at 0."""
    with _pg_cur() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, first_seen_at, last_seen_at) VALUES "
            "('projNeverCost', 'projNeverCost', now(), now()), "
            "('projOldCost', 'projOldCost', now(), now()), "
            "('projRecentCost', 'projRecentCost', now(), now())"
        )
        cur.execute(
            "INSERT INTO usage_rollup (session_id, project_id, hour, model, is_main, "
            "first_ts, last_ts, requests, cost_usd) VALUES "
            # Never cost anything, ever — must be excluded outright, even
            # though it has a usage_rollup row (a row with cost 0 is not
            # the same as "no usage").
            "('sess-zero', 'projNeverCost', now() - interval '1 day', 'm', TRUE, now(), now(), 1, 0), "
            # Real historical cost (bigger than projRecentCost's), but 60
            # days old — outside a 7d range. Must still be LISTED (all-time
            # cost is nonzero) but sorted to the bottom with 0 range cost.
            "('sess-old', 'projOldCost', now() - interval '60 days', 'm', TRUE, now(), now(), 1, 5.00), "
            # Smaller all-time cost, but entirely inside the 7d range —
            # must outrank projOldCost despite the smaller all-time total,
            # proving the ordering is RANGE-scoped, not all-time.
            "('sess-recent', 'projRecentCost', now() - interval '1 hour', 'm', TRUE, now(), now(), 1, 1.00)"
        )

    r = app_with_fresh_data.get("/api/projects?range=7d")
    assert r.status_code == 200
    body = r.json()
    by_id = {p["project_id"]: p for p in body["projects"]}

    assert "projNeverCost" not in by_id, \
        "all-time-zero-cost project must be excluded, not just range-filtered"
    assert "projOldCost" in by_id, \
        "a historically-costly project must stay listed even at 0 range cost"
    assert by_id["projOldCost"]["total_cost"] == 0.0
    assert by_id["projRecentCost"]["total_cost"] == 1.0

    pids_in_order = [p["project_id"] for p in body["projects"]]
    assert pids_in_order.index("projRecentCost") < pids_in_order.index("projOldCost"), (
        "ordering must follow range-scoped cost, not all-time cost — "
        "projOldCost's larger all-time total must NOT outrank projRecentCost"
    )

    # Widening the range to include projOldCost's usage re-sorts it above
    # projRecentCost — proving the list is genuinely range-scoped, not a
    # fixed order computed once.
    r_wide = app_with_fresh_data.get("/api/projects?range=90d")
    assert r_wide.status_code == 200
    body_wide = r_wide.json()
    by_id_wide = {p["project_id"]: p for p in body_wide["projects"]}
    assert by_id_wide["projOldCost"]["total_cost"] == 5.0
    pids_wide = [p["project_id"] for p in body_wide["projects"]]
    assert pids_wide.index("projOldCost") < pids_wide.index("projRecentCost")


def test_cache_per_model_shape(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "per_model" in body and "session_total" in body
    assert "top_output" in body and "top_cache_read" in body
    if body["per_model"]:
        m = body["per_model"][0]
        assert {"model", "turns", "fresh", "cache_read", "output",
                "hit_rate_pct", "cost_total", "cost_buckets"} <= set(m)
        assert {"fresh", "read", "output"} == set(m["cost_buckets"])


def test_cache_per_model_reports_whether_the_rate_was_matched(app_with_data):
    """Cost computed from assumed rates must arrive labelled as such, or the
    UI renders a guess as a billed fact. Every fixture model is a canonical
    label, so nothing here is estimated; pricing's own tests cover the
    unmatched side.
    """
    body = app_with_data.get("/api/cache?range=3650d").json()
    assert body["per_model"], "fixture produced no per-model rows"
    for m in body["per_model"]:
        assert m["estimated_rate"] is False, m["model"]


def test_cache_dedups_cross_file_uuid(app_with_data):
    """sess-C main + subagent peer + sess-D main all have uuid='shared-uuid-1'.
    Records table holds 3 rows for that uuid; DISTINCT ON dedups to 1 in the
    per_model totals.

    sess-C main has input=1000, output=500 (single record).
    sess-C subagent has input=1000, output=500 (same uuid -> dedup'd).
    sess-D main has 2 records: shared-uuid-1 (1000/500, dedup'd) +
                                sess-D-only (50/25, kept).

    After cross-file dedup:
      shared-uuid-1 winner = lexicographically-first file_key, which is
      sessions/projB/sess-C/subagents/agent-aaaa/wire.jsonl.
      One row claims the shared uuid; the other two drop. The remaining tally
      for projB: 1000 + 50 input, 500 + 25 output.
    """
    r = app_with_data.get("/api/cache?range=3650d&project=projB")
    body = r.json()
    assert body["session_total"]["fresh"] == 1050
    assert body["session_total"]["output"] == 525
    assert body["session_total"]["turns"] == 2


def test_cache_top_n_limited_to_10(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    body = r.json()
    assert len(body["top_output"]) <= 10
    assert len(body["top_cache_read"]) <= 10


def test_cache_bad_range_400(app_with_data):
    r = app_with_data.get("/api/cache?range=abc")
    assert r.status_code == 400


def test_cache_session_total_matches_per_model_sum(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    body = r.json()
    sum_turns = sum(m["turns"] for m in body["per_model"])
    sum_cost = round(sum(m["cost_total"] for m in body["per_model"]), 4)
    assert body["session_total"]["turns"] == sum_turns
    assert body["session_total"]["cost_total"] == sum_cost


def test_cache_session_total_estimated_rate_true_when_any_model_estimated(
    app_with_two_models, monkeypatch
):
    """app_with_two_models carries kimi-k2-6 (the injected early session)
    alongside kimi-k2-7-code (everything else). Both are exact matches
    normally; drop kimi-k2-6 from the rate table so it resolves as
    "default" (estimated) while kimi-k2-7-code stays exact — a genuine
    mixed session.
    """
    patched = {k: v for k, v in pricing.MODEL_RATES.items() if k != "kimi-k2-6"}
    monkeypatch.setattr(pricing, "MODEL_RATES", patched)

    body = app_with_two_models.get("/api/cache?range=3650d").json()
    assert len(body["per_model"]) >= 2, body["per_model"]
    flags = {m["model"]: m["estimated_rate"] for m in body["per_model"]}
    assert flags["kimi-k2-6"] is True
    assert flags["kimi-k2-7-code"] is False
    assert body["session_total"]["estimated_rate"] is True


def test_cache_session_total_estimated_rate_false_when_all_exact(app_with_two_models):
    body = app_with_two_models.get("/api/cache?range=3650d").json()
    assert len(body["per_model"]) >= 2, body["per_model"]
    assert all(m["estimated_rate"] is False for m in body["per_model"])
    assert body["session_total"]["estimated_rate"] is False


def test_cache_session_total_estimated_rate_false_for_empty_per_model(app_with_data):
    """An empty per_model list must not crash any(...) over it, and must not
    default-True a total with no contributing models."""
    r = app_with_data.get("/api/cache?range=3650d&model=nonexistent-model-zzz")
    assert r.status_code == 200
    body = r.json()
    assert body["per_model"] == []
    assert body["session_total"]["estimated_rate"] is False


def test_transcript_streams(app_with_data):
    r = app_with_data.get("/api/sessions/sess-A/transcript")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-ndjson"
    first = r.text.split("\n")[0]
    obj = json.loads(first)
    # Kimi wire.jsonl puts the event type under message.type.
    assert "message" in obj and "type" in obj["message"]


def test_transcript_etag_header(app_with_data):
    r = app_with_data.get("/api/sessions/sess-A/transcript")
    assert "etag" in {k.lower() for k in r.headers.keys()}


def test_transcript_404(app_with_data):
    r = app_with_data.get("/api/sessions/does-not-exist/transcript")
    assert r.status_code == 404


def test_sidecar_path_validation(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "data/tool-results/x.txt"},
    )
    assert r.status_code == 200
    assert r.text.strip() == "tool output"
    r2 = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "../../../etc/passwd"},
    )
    assert r2.status_code == 400


def test_sidecar_absolute_path_rejected(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "/etc/passwd"},
    )
    assert r.status_code == 400


def test_sidecar_missing_file_404(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "data/does-not-exist.txt"},
    )
    assert r.status_code == 404


def test_context_growth_agg_shape(app_with_data):
    r = app_with_data.get("/api/context-growth/agg?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "per_turn" in body and "per_session_final" in body
    for k in ("n", "mean", "p50", "p90", "p99", "max"):
        assert k in body["per_turn"]
        assert k in body["per_session_final"]


def test_context_growth_session_returns_canonical_array(app_with_data):
    """Mini fixture sess-A has 1 turn (single TurnBegin->StatusUpdate->TurnEnd).
    Verify the per-turn array is returned with the canonical shape."""
    r = app_with_data.get("/api/context-growth/session/sess-A")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "sess-A"
    assert "turns" in body and isinstance(body["turns"], list)
    if body["turns"]:
        t = body["turns"][0]
        assert {"idx", "ts", "line", "input", "output", "delta"} == set(t)
    assert body["total_turns"] == len(body["turns"])


def test_context_growth_session_404(app_with_data):
    r = app_with_data.get("/api/context-growth/session/does-not-exist")
    assert r.status_code == 404


def test_tool_error_rate_returns_expected_shape(app_with_data):
    r = app_with_data.get("/api/tool-error-rate?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "range" in body
    assert "bucket_s" in body
    assert "buckets" in body
    assert isinstance(body["buckets"], list)
    for b in body["buckets"]:
        assert {"ts", "model", "tool", "n_total", "n_error"} <= set(b.keys())
        assert b["n_error"] <= b["n_total"]


def test_projects_groups_unresolved(app_with_unresolved):
    r = app_with_unresolved.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    pids = sorted(p["project_id"] for p in body["projects"])
    assert pids == ["<unresolved>", "projA", "projB"]
    unresolved = next(p for p in body["projects"] if p["project_id"] == "<unresolved>")
    assert unresolved["display_name"] == "<unresolved>"
    assert unresolved["session_count"] == 2


def test_unresolved_filter_scopes_queries(app_with_unresolved):
    r_all = app_with_unresolved.get("/api/cache?range=3650d")
    assert r_all.status_code == 200
    all_total = r_all.json()["session_total"]

    r_unres = app_with_unresolved.get(
        "/api/cache", params={"range": "3650d", "project": "<unresolved>"}
    )
    assert r_unres.status_code == 200
    unres_total = r_unres.json()["session_total"]
    assert unres_total["turns"] > 0
    assert unres_total["cost_total"] > 0
    assert unres_total["turns"] < all_total["turns"]
    assert unres_total["cost_total"] < all_total["cost_total"]

    # Junk hash-projects must not leak into a normal project's filtered numbers.
    # These are the documented projB totals under the plain fixture.
    r_projB = app_with_unresolved.get(
        "/api/cache", params={"range": "3650d", "project": "projB"}
    )
    projB_total = r_projB.json()["session_total"]
    assert projB_total["turns"] == 2
    assert projB_total["fresh"] == 1050
    assert projB_total["output"] == 525
    assert projB_total["cost_total"] == 0.0031


def test_projects_unaffected_without_junk(app_with_data):
    r = app_with_data.get("/api/projects")
    assert r.status_code == 200
    pids = [p["project_id"] for p in r.json()["projects"]]
    assert "<unresolved>" not in pids


# ---------------------------------------------------------------- heatmap

def _insert_tz_probe_rows():
    """Two records with a unique model, one in winter (CET, UTC+1) and one
    in summer (CEST, UTC+2), to prove the endpoint is DST-aware."""
    with _pg_cur() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, first_seen_at, last_seen_at) "
            "VALUES ('projTZ', 'projTZ', now(), now()) ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, r2_etag, "
            "r2_size_bytes, r2_last_modified, parsed_at, parser_version) "
            "VALUES ('projTZ/tz.jsonl', 'projTZ', 'tzsess', TRUE, 'etag-tz', 1, now(), now(), 'test')"
        )
        cur.execute(
            "INSERT INTO records (file_key, line_num, uuid, ts, model, output_tokens, cost_usd) VALUES "
            # 2026-01-15 is a Thursday (ISODOW 4); 10:30Z in CET (UTC+1) is 11:30 local.
            "('projTZ/tz.jsonl', 1, 'uuid-tz-winter', '2026-01-15T10:30:00Z', 'tz-probe-model', 10, 0.01), "
            # 2026-07-15 is a Wednesday (ISODOW 3); 10:30Z in CEST (UTC+2) is 12:30 local.
            "('projTZ/tz.jsonl', 2, 'uuid-tz-summer', '2026-07-15T10:30:00Z', 'tz-probe-model', 20, 0.02)"
        )

    # /api/activity-heatmap reads usage_rollup, which ingest rebuilds from
    # `records`. These rows were inserted behind ingest's back, so rebuild
    # it here or the endpoint cannot see them (SV-ROLLUP: the rollup is
    # derived state; anything mutating `records` outside ingest must
    # rebuild it).
    ingest.rebuild_rollup()


def test_activity_heatmap_shape(app_with_data):
    r = app_with_data.get("/api/activity-heatmap?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert body["tz"] == "Europe/Prague"
    assert body["cells"], "mini fixture must produce at least one cell"
    for c in body["cells"]:
        assert 1 <= c["dow"] <= 7
        assert 0 <= c["hour"] <= 23
        assert c["requests"] >= 1
        assert c["output_tokens"] >= 0
        assert c["cost_usd"] >= 0


def test_activity_heatmap_requests_match_dashboard(app_with_data):
    # Both endpoints read through the same DISTINCT ON (uuid) dedup, so
    # total request counts must agree for the same range.
    heat = app_with_data.get("/api/activity-heatmap?range=3650d").json()
    dash = app_with_data.get("/api/dashboard?range=3650d").json()
    assert sum(c["requests"] for c in heat["cells"]) == \
           sum(h["requests"] for h in dash["hourly"])


def test_activity_heatmap_dst_awareness(app_with_fresh_data):
    # Inserts rows and rebuilds the rollup, so it needs its own database —
    # the module-scoped client is shared with every read-only test.
    _insert_tz_probe_rows()
    r = app_with_fresh_data.get("/api/activity-heatmap?range=3650d&model=tz-probe-model")
    assert r.status_code == 200
    cells = {(c["dow"], c["hour"]): c for c in r.json()["cells"]}
    assert set(cells) == {(4, 11), (3, 12)}, cells
    assert cells[(4, 11)]["requests"] == 1   # winter: 10:30Z -> 11:30 CET, Thu
    assert cells[(3, 12)]["requests"] == 1   # summer: 10:30Z -> 12:30 CEST, Wed
    assert cells[(3, 12)]["output_tokens"] == 20


def test_activity_heatmap_project_filter(app_with_data):
    both = app_with_data.get("/api/activity-heatmap?range=3650d").json()
    one = app_with_data.get("/api/activity-heatmap?range=3650d&project=projA").json()
    assert sum(c["requests"] for c in one["cells"]) < \
           sum(c["requests"] for c in both["cells"])


def test_activity_heatmap_bad_range_400(app_with_data):
    assert app_with_data.get("/api/activity-heatmap?range=bogus").status_code == 400


def test_dashboard_response_is_cached_and_fresh_bypasses(app_with_fresh_data):
    cache.response_cache.clear()
    first = app_with_fresh_data.get("/api/dashboard?range=all").json()

    # Mutate the DB underneath the cache: delete every record. usage_rollup
    # is derived state that ingest rebuilds from `records`, so emptying the
    # data means emptying both — leaving the rollup behind would just be
    # reading a stale pre-aggregate, which is not what this test is about.
    with db.viz_conn() as c:
        c.execute("DELETE FROM records")
        c.execute("DELETE FROM usage_rollup")

    cached = app_with_fresh_data.get("/api/dashboard?range=all").json()
    assert cached == first                       # stale-but-cached payload

    fresh = app_with_fresh_data.get("/api/dashboard?range=all&fresh=1").json()
    assert fresh["cost_by_model"] == []          # fresh=1 sees the empty DB


def test_dashboard_cost_by_project_shape(app_with_data):
    """cost_by_project mirrors cost_by_model: range-filtered, sorted by
    cost desc, zero-cost rows excluded, at most 10 project rows plus a
    single "Other (N projects)" fold."""
    body = app_with_data.get("/api/dashboard?range=3650d").json()
    cbp = body["cost_by_project"]
    assert {"projA", "projB"} <= {r["project"] for r in cbp}
    costs = [r["cost_usd"] for r in cbp]
    assert all(c > 0 for c in costs)
    assert costs == sorted(costs, reverse=True)
    named = [r for r in cbp if not r["project"].startswith("Other (")]
    assert len(named) <= 10
    others = [r for r in cbp if r["project"].startswith("Other (")]
    assert len(others) <= 1

    # A project filter scopes the breakdown to that one project.
    body_b = app_with_data.get("/api/dashboard?range=3650d&project=projB").json()
    assert {r["project"] for r in body_b["cost_by_project"]} == {"projB"}


def test_dashboard_cost_by_project_omitted_for_guest(app_with_data):
    """The guest gate is SERVER-side. /api/dashboard is guest-accessible,
    so per-project names/costs — the very data the 403s on /api/projects
    and on project= exist to withhold (session.auth_middleware) — must be
    missing from the response body itself, not merely unrendered by the
    frontend.

    This suite mounts the api routers without auth (auth bypassed), so a
    guest is simulated by a middleware setting the SAME
    request.state.is_guest flag the real middleware sets. The guest call
    reuses the non-guest call's query params, so it is served from the
    SHARED response cache — proving the strip happens per-request,
    outside the cached payload."""
    body = app_with_data.get("/api/dashboard?range=3650d").json()
    assert "cost_by_project" in body

    a = FastAPI()

    @a.middleware("http")
    async def set_guest_flag(request: Request, call_next):
        request.state.is_guest = True
        return await call_next(request)

    a.include_router(api_dash_mod.router)
    guest = TestClient(a).get("/api/dashboard?range=3650d")
    assert guest.status_code == 200
    assert "cost_by_project" not in guest.json()
    # The rest of the payload is untouched.
    assert "cost_by_model" in guest.json()


# --------------------------------------------------- rollup == live path

def _insert_latency_probe_rows():
    """Seed enough latencies in one bucket to cross the outlier threshold.

    The mini mirror yields a single record with a reply_latency_s, so the
    outlier branch (only buckets with n >= 100 get dots) would never run.
    150 rows in one hour means 1% is 2 dots per end — enough to catch a
    floor-vs-ceil cutoff, which is exactly the bug this rollup invites.
    """
    with _pg_cur() as cur:
        cur.execute(
            "INSERT INTO records (file_key, line_num, uuid, ts, model, "
            "                     output_tokens, cost_usd, reply_latency_s) "
            "SELECT f.file_key, 8000 + i, 'lat-probe-' || i, "
            "       TIMESTAMPTZ '2026-02-03 04:00:00+00' + make_interval(secs => i), "
            "       'lat-probe-model', 1, 0, i * 0.5 "
            "FROM files f, generate_series(1, 150) AS i "
            "WHERE f.file_key = (SELECT MIN(file_key) FROM files)"
        )

    ingest.recompute_canonical()
    ingest.rebuild_latency_rollup()


def test_reply_latency_rollup_matches_live_path(app_with_fresh_data, monkeypatch):
    """latency_rollup must return exactly what the live query returns.

    The rollup exists because percentiles cannot be summed across buckets;
    it is only correct because the display buckets are epoch-aligned. That
    is an easy thing to get subtly wrong (an off-by-one in the outlier
    cutoff, a bucket-centering mismatch), so compare the two paths rather
    than trusting the port.
    """
    _insert_latency_probe_rows()

    rolled = app_with_fresh_data.get("/api/reply-latency?range=3650d").json()
    assert rolled["bands"], "probe rows must produce bands"
    assert rolled["outliers"], "probe bucket must cross the n >= 100 dot threshold"

    # Empty the eligible-width list so the same request takes the live path.
    monkeypatch.setattr(api_mod, "_LATENCY_ROLLUP_BUCKETS", ())
    cache.response_cache.clear()
    live = app_with_fresh_data.get("/api/reply-latency?range=3650d").json()

    assert rolled["bands"] == live["bands"]
    assert rolled["outliers"] == live["outliers"]


def _insert_tool_probe_rows():
    """Seed tool calls with a known error mix.

    The mini R2 mirror produces no tool_uses at all, so without this the
    rollup-vs-live comparison below would compare two empty lists and
    prove nothing. Settled (is_error NOT NULL) and unsettled calls are
    both present because the two endpoints count different populations:
    tool-usage counts all of them, tool-error-rate only the settled ones.
    """
    with _pg_cur() as cur:
        cur.execute(
            "INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, is_error) "
            "SELECT f.file_key, 9000 + i, 0, "
            "       TIMESTAMPTZ '2026-03-04 05:06:07+00' + make_interval(hours => i), "
            "       CASE WHEN mod(i, 2) = 0 THEN 'Read' ELSE 'Bash' END, "
            "       CASE WHEN mod(i, 5) = 0 THEN TRUE "
            "            WHEN mod(i, 7) = 0 THEN NULL ELSE FALSE END "
            "FROM files f, generate_series(1, 30) AS i "
            "WHERE f.file_key = (SELECT MIN(file_key) FROM files)"
        )

    ingest.rebuild_tool_rollup()


def test_tool_endpoints_rollup_matches_live_path(app_with_fresh_data, monkeypatch):
    """tool_rollup must return exactly what the live per-call query does."""
    _insert_tool_probe_rows()

    rolled_usage = app_with_fresh_data.get("/api/tool-usage?range=3650d").json()
    rolled_errors = app_with_fresh_data.get("/api/tool-error-rate?range=3650d").json()
    assert rolled_usage["buckets"], "probe rows must reach /api/tool-usage"
    assert rolled_errors["buckets"], "probe rows must reach /api/tool-error-rate"
    # Unsettled calls exist, so the two endpoints must NOT see the same totals.
    assert (sum(b["n"] for b in rolled_usage["buckets"])
            > sum(b["n_total"] for b in rolled_errors["buckets"]))

    live_source = _tool_source(60)      # the live subquery branch
    monkeypatch.setattr(api_mod, "_tool_source", lambda bucket_s: live_source)
    cache.response_cache.clear()
    live_usage = app_with_fresh_data.get("/api/tool-usage?range=3650d").json()
    live_errors = app_with_fresh_data.get("/api/tool-error-rate?range=3650d").json()

    assert rolled_usage["buckets"] == live_usage["buckets"]
    assert rolled_errors["buckets"] == live_errors["buckets"]


def _insert_churn_probe_rows():
    """Seed tool_uses rows with KNOWN edit churn at three recent hours.

    Same SV-ROLLUP drill as _insert_tool_probe_rows: the rows are written
    straight into tool_uses and the rollup is rebuilt by hand, because the
    mini R2 mirror carries no tool calls at all. Totals: 60 added, 6
    deleted (10*i / i for i in 1..3).
    """
    with _pg_cur() as cur:
        cur.execute(
            "INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name,"
            " is_error, lines_added, lines_deleted) "
            "SELECT f.file_key, 9500 + i, 0,"
            "       date_trunc('hour', now()) - make_interval(hours => i),"
            "       'Edit', FALSE, 10 * i, i "
            "FROM files f, generate_series(1, 3) AS i "
            "WHERE f.file_key = (SELECT MIN(file_key) FROM files)"
        )

    ingest.rebuild_tool_rollup()


def test_dashboard_churn_rollup_and_live_paths_agree(app_with_fresh_data, monkeypatch):
    """The churn series must be identical off tool_rollup (>= 1h buckets)
    and off the live tool_uses subquery — the one dual-path source behind
    both, same as the tool endpoints."""
    _insert_churn_probe_rows()

    body = app_with_fresh_data.get("/api/dashboard?range=3650d").json()
    churn = body["churn"]
    assert churn, "probe churn must reach /api/dashboard"
    assert sum(c["lines_added"] for c in churn) == 60
    assert sum(c["lines_deleted"] for c in churn) == 6

    # Project filter applies (the probe rows hang off projA's file).
    proj = app_with_fresh_data.get("/api/dashboard?range=3650d&project=projA").json()
    assert sum(c["lines_added"] for c in proj["churn"]) == 60
    other = app_with_fresh_data.get("/api/dashboard?range=3650d&project=projB").json()
    assert sum(c["lines_added"] for c in other["churn"]) == 0

    live_source = _tool_source(60)      # the live subquery branch
    monkeypatch.setattr(api_dash_mod, "_tool_source", lambda bucket_s: live_source)
    cache.response_cache.clear()
    live = app_with_fresh_data.get("/api/dashboard?range=3650d").json()
    assert live["churn"] == churn


def test_dashboard_churn_subhour_range_reads_live_tool_uses(app_with_fresh_data):
    """The 24h view buckets below tool_rollup's hourly grain, so its churn
    comes from the live subquery — exercise that branch for real."""
    _insert_churn_probe_rows()

    body = app_with_fresh_data.get("/api/dashboard?range=1d").json()
    assert body["bucket_s"] < 3600, "1d must bucket below the rollup grain"
    assert sum(c["lines_added"] for c in body["churn"]) == 60
    assert sum(c["lines_deleted"] for c in body["churn"]) == 6


def test_dashboard_churn_empty_without_tool_rows(app_with_data):
    """No-churn case: the mini R2 mirror produces no tool_uses, so the
    series is present but empty — the panels render a flat zero, not an
    error."""
    body = app_with_data.get("/api/dashboard?range=all").json()
    assert body["churn"] == []


def test_dashboard_model_filter_does_not_500(app_with_data):
    """?model= must filter, not raise.

    The queries that read `files` (file_counts, ctx_traces, ctx_lines,
    rate_limits) carry no model placeholder, but were handed the same arg
    list as the model-filtered ones — one argument more than the statement
    had placeholders, so every ?model= request raised. Both the rollup
    path (wide buckets) and the live path (24h, 5-minute buckets) are
    exercised, since they build their args separately.
    """
    unfiltered = app_with_data.get("/api/dashboard?range=3650d").json()
    present = sorted({h["model"] for h in unfiltered["hourly"]})
    assert present, "fixture must produce at least one model"

    for rng in ("3650d", "1d"):
        # A model the fixture DOES have: 200, and nothing else leaks in.
        r = app_with_data.get(f"/api/dashboard?range={rng}&model={present[0]}")
        assert r.status_code == 200, f"range={rng}: {r.status_code} {r.text[:200]}"
        assert {h["model"] for h in r.json()["hourly"]} <= {present[0]}

        # A model it does NOT: also 200, and empty — proving the filter is
        # applied rather than ignored, which one model alone cannot show.
        r = app_with_data.get(f"/api/dashboard?range={rng}&model=no-such-model")
        assert r.status_code == 200, f"range={rng}: {r.status_code} {r.text[:200]}"
        assert r.json()["hourly"] == []


def test_rate_limit_hits_are_filtered_on_the_hits_own_ts(app_with_fresh_data):
    """A file touched inside the range can carry hits far older than it.

    The query filtered on files.r2_last_modified alone, so re-archiving a
    transcript today dragged every rate-limit hit it ever recorded into a
    7-day view. The hit's own `ts` is what the panel plots, so that is what
    must be filtered.
    """
    with _pg_cur() as cur:
        cur.execute(
            "UPDATE files SET r2_last_modified = now(), "
            "       rate_limit_hits = '[{\"ts\":\"2025-07-26T10:00:00Z\","
            "                            \"content\":\"old hit\"}]'::jsonb "
            " WHERE file_key = (SELECT MIN(file_key) FROM files WHERE is_main)"
        )

    cache.response_cache.clear()
    short = app_with_fresh_data.get("/api/dashboard?range=7d").json()
    assert [h for h in short["rate_limit_hits"] if h["content"] == "old hit"] == [], \
        "a >1y old hit must not appear in a 7-day view"

    cache.response_cache.clear()
    wide = app_with_fresh_data.get("/api/dashboard?range=3650d").json()
    assert [h["content"] for h in wide["rate_limit_hits"] if h["content"] == "old hit"] \
        == ["old hit"], "and the file's mtime clause must still let it through"


def test_a_malformed_hit_ts_does_not_take_down_the_dashboard(app_with_fresh_data):
    """Junk in a hit's `ts` must be excluded, not raised.

    Casting it is what filters on the hit's own time, but an unguarded
    `::timestamptz` RAISES on a malformed value, and the traceback escapes
    the handler — so one bad `ts` anywhere in files.rate_limit_hits would
    500 the entire dashboard, every panel, not merely this one.

    The second block below is the one that matters: those values are
    timestamp-SHAPED and still raise on cast, so a guard that pattern-
    matches the shape passes them straight through to the cast it was
    meant to protect. Only real input validation excludes them.
    """
    with _pg_cur() as cur:
        cur.execute(
            "UPDATE files SET r2_last_modified = now(), "
            "       rate_limit_hits = %s::jsonb "
            " WHERE file_key = (SELECT MIN(file_key) FROM files WHERE is_main)",
            (json.dumps([
                # Not timestamp-shaped at all.
                {"ts": "not-a-timestamp", "content": "junk ts"},
                {"ts": 123, "content": "numeric ts"},
                {"ts": "", "content": "empty ts"},
                {"ts": None, "content": "null ts"},
                {"content": "no ts key at all"},
                # Timestamp-shaped, but not valid timestamps.
                {"ts": "2026-13-45T99:99:99Z", "content": "month 13, day 45"},
                {"ts": "2026-02-30T00:00:00Z", "content": "30th of february"},
                {"ts": "2026-01-01 25:00:00Z", "content": "hour 25"},
                {"ts": "2026-01-01T00:00:00Z lolwat", "content": "trailing junk"},
                # Valid, and on either side of the range boundary. The
                # in-range one is computed RELATIVE TO NOW: pinned to an
                # absolute date it silently stops being inside `range=30d`
                # once the calendar passes it, and the test then fails for
                # a reason that has nothing to do with what it checks.
                # That is exactly what happened here — it was pinned to
                # 2026-07-20 and began failing on 2026-08-19.
                {"ts": (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"), "content": "good hit"},
                {"ts": "1998-01-01T00:00:00Z", "content": "too old"},
            ]),),
        )

    cache.response_cache.clear()
    r = app_with_fresh_data.get("/api/dashboard?range=30d")
    assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
    assert [h["content"] for h in r.json()["rate_limit_hits"]] == ["good hit"]

# pylint: disable=import-outside-toplevel,redefined-outer-name
# import-outside-toplevel: the backend.* imports inside the fixtures below
# are deferred ON PURPOSE — the setdefaults below must land in os.environ
# before any backend module that reads env is imported, or the guard is
# void. Do not hoist them. redefined-outer-name: the fixture-argument
# pattern (a fixture taking another fixture by name) is standard pytest;
# the fixture names are part of the shared contract consumed by other test
# modules and must not change.
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# Imported at module top like the linters want. The env claims below must
# only precede imports of backend modules that READ env at import time
# (backend.app loads .env); backend.cache reads none, so it is safe here.
from backend import cache

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# ...and this directory, so one test module can import another's fixture
# instead of duplicating an expensive fresh-DB + mini-R2 setup.
sys.path.insert(0, str(Path(__file__).resolve().parent))

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Per-run suffix for every scratch database name: stable within one pytest
# process, unique across runs. Two concurrent runs — of this suite, or of
# this suite and a sibling repo's against the same Postgres — must never
# share a scratch database: a fixed name lets one run's dropdb/createdb
# destroy the other's database mid-test. Prefixes stay recognisable, and
# setup/teardown compute the name from this same constant, so teardown
# drops exactly what setup created.
_RUN_SUFFIX = f"{os.getpid():x}-{uuid.uuid4().hex[:8]}"


def run_db_name(prefix: str) -> str:
    """This run's scratch database name for a recognisable prefix."""
    return f"{prefix}_{_RUN_SUFFIX}"


_TEST_AUTH_DB = run_db_name("kimimeter_test_auth")

# Claim DATABASE_URL_VIZ before anything can load the repo's .env.
#
# backend/app.py calls db.load_dotenv(".env") at IMPORT time, and .env points
# DATABASE_URL_VIZ at the live kimimeter database. load_dotenv only ever
# setdefault()s, so whoever sets the variable first wins — and conftest is
# imported before any test module. Claiming it here is therefore enough to
# keep a test process off production even if some module (now or later)
# imports backend.app at module scope.
#
# The ordering is load-bearing: this must run above any import of a backend
# module that reads env at import time. It is a backstop, not a replacement —
# DB-touching tests still monkeypatch their own scratch database.
os.environ.setdefault("DATABASE_URL_VIZ", f"postgresql:///{run_db_name('kimimeter_test')}")
# DATABASE_URL_AUTH gets the same guard: .env pins it at the live auth
# database, and without this setdefault any test that touches auth outside
# the auth_db fixture (e.g. a `pytest -k` partial run) would read from —
# and now that login records sessions, WRITE to — the production database.
os.environ.setdefault("DATABASE_URL_AUTH", f"postgresql:///{_TEST_AUTH_DB}")
# Force file-mode R2 for unit tests; pytest never hits real R2.
os.environ.setdefault("R2_ENDPOINT", "file:///tmp/kd-test-r2/")
os.environ.setdefault("R2_BUCKET", "kimi")
os.environ.setdefault("R2_ACCOUNT_ID", "")
os.environ.setdefault("R2_ACCESS_KEY_ID", "")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "")
os.environ.setdefault("PARSER_VERSION", "test")
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
# TestClient runs over plain HTTP — Secure-flag cookies would never come back.
# A forced assignment, NOT a setdefault: as with the DATABASE_URL guards
# above, a setdefault here would beat .env (conftest is imported before
# backend.app's load_dotenv runs), but not a COOKIE_SECURE=1 exported in
# the developer's or CI shell. A Secure cookie is never returned by the
# test client's jar over plain http.
os.environ["COOKIE_SECURE"] = "0"
# SESSION_COOKIE_DOMAIN from .env (or the ambient environment) would make
# the app issue domain-scoped cookies the test client's jar never returns
# for the `testserver` host, failing auth tests for reasons unrelated to
# any defect. Force it empty: backend/session.py maps "" to None, so
# cookies stay host-only. A forced assignment, NOT a setdefault: a
# setdefault would suffice against .env alone (same ordering argument as
# the DATABASE_URL guards above), but an ambient exported value would
# still win — measured: with setdefaults here, exporting
# SESSION_COOKIE_DOMAIN=.example.invalid fails 10 tests. Tests that
# exercise the rollout boundary monkeypatch the variable themselves and
# are unaffected.
os.environ["SESSION_COOKIE_DOMAIN"] = ""
# GUEST_SESSION_SECRET gets the same forced-empty treatment: an ambient
# exported value would change which secret signs guest tokens mid-suite,
# failing guest tests for reasons unrelated to any defect.
# backend/session.py maps ""/whitespace to the process-local fallback, so
# guest behaviour under test is the unconfigured behaviour.
os.environ["GUEST_SESSION_SECRET"] = ""
# No background cache warming under test: a warm queued by run_ingest
# outlives the fixture that created its DB, and its queries then race the
# teardown that drops it — producing failures in unrelated tests.
os.environ["KIMIMETER_WARM_CACHE"] = "0"


@pytest.fixture(autouse=True)
def _reset_response_cache():
    # response_cache is a process-global. Two tests with different fixtures
    # but identical query params would otherwise read each other's payloads.
    cache.response_cache.clear()
    yield
    cache.response_cache.clear()


_TEST_SECRET = "fixture-session-secret-0123456789"
_TEST_UID = 4242


@pytest.fixture(scope="module")
def auth_db():
    """A scratch auth DB with web_sessions and one seeded user row.

    Module-scoped: rows written by one test persist for the rest of the
    module. Two rules keep tests from seeing each other's state: nonces
    must be disjoint per test, always; and any test that asserts over
    list_sessions must use a private user_id (not _TEST_UID), since that
    query is keyed by user alone."""
    os.system(f"dropdb --if-exists {_TEST_AUTH_DB} 2>/dev/null")
    os.system(f"createdb {_TEST_AUTH_DB} 2>/dev/null")
    subprocess.run(
        ["psql", _TEST_AUTH_DB, "-c",
         "CREATE TABLE users (user_id BIGINT PRIMARY KEY, config JSONB NOT NULL)"],
        check=True, stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["psql", _TEST_AUTH_DB, "-f", str(_REPO_ROOT / "backend/schema_auth.sql")],
        check=True, stdout=subprocess.DEVNULL,
    )
    os.environ["DATABASE_URL_AUTH"] = f"postgresql:///{_TEST_AUTH_DB}"
    from backend import db
    db.reset_auth_pool()
    yield
    db.reset_auth_pool()
    os.system(f"dropdb --if-exists {_TEST_AUTH_DB} 2>/dev/null")


@pytest.fixture
def seeded_user(auth_db):
    """(user_id, session_secret) for a user row that exists in the auth DB."""
    import json
    from backend import auth, db, session as session_mod
    config = {session_mod.WEB_SESSION_SECRET_KEY: _TEST_SECRET}
    auth.set_web_password(config, "fixture-password")
    with db.auth_conn() as c:
        c.execute(
            "INSERT INTO users (user_id, config) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (user_id) DO UPDATE SET config = EXCLUDED.config",
            (_TEST_UID, json.dumps(config)),
        )
    return _TEST_UID, _TEST_SECRET


@pytest.fixture
def logged_in_client(seeded_user):
    """TestClient that has completed a real POST /login."""
    from fastapi.testclient import TestClient
    from backend.app import app
    uid, _ = seeded_user
    client = TestClient(app)
    resp = client.post(
        "/login",
        data={"user_id": str(uid), "password": "fixture-password"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client

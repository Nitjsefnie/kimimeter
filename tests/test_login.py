import ast
import contextlib
import re
from http.cookies import Morsel, SimpleCookie
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from backend import auth
from backend import db
from backend import login as login_mod
from backend import session as session_mod
from backend import sessions_repo
from backend.login import _LOGIN_FAILURES


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """The login module's rate-limit dict is process-global; clear between tests
    so test_session_cookie_round_trip doesn't inherit failures from
    test_rate_limit_after_5_failures (both POST from the same TestClient host).
    """
    _LOGIN_FAILURES.clear()
    yield
    _LOGIN_FAILURES.clear()


@pytest.fixture(name="app")
def _app(monkeypatch):
    """Build a fresh FastAPI app per test, with the auth DB mocked."""
    a = FastAPI()
    a.middleware("http")(session_mod.auth_middleware)
    a.include_router(login_mod.router)

    @a.get("/api/me")
    def me(request: Request):
        return {"user_id": request.state.user_id}

    return a


@pytest.fixture(name="fake_user")
def _fake_user(monkeypatch):
    """Stub the auth DB with one user that has a known password."""
    config: dict = {}
    auth.set_web_password(config, "hunter2")
    store = {12345: config}

    def _load(user_id, **_kwargs):
        return store.get(user_id)

    def _write(user_id, cfg):
        store[user_id] = cfg

    def _exists(user_id):
        return user_id in store

    monkeypatch.setattr(session_mod, "load_user_config", _load)
    monkeypatch.setattr(session_mod, "write_user_config", _write)
    monkeypatch.setattr(login_mod, "user_exists", _exists)
    # Session rows go to the real auth DB, which these tests don't have;
    # row recording is covered end-to-end in test_sessions_repo.py.
    monkeypatch.setattr(
        sessions_repo, "record_session", lambda *args, **kwargs: None
    )
    # resolve_session_user_id now checks the nonce against web_sessions;
    # with no real auth DB here, treat every nonce as active. The reject
    # path is covered against a real DB in test_sessions_repo.py.
    monkeypatch.setattr(
        sessions_repo, "is_session_active", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        sessions_repo, "touch_session", lambda *args, **kwargs: None
    )

    # resolve_session_user_id opens one shared auth_conn() around the
    # (stubbed) config/nonce lookups; yield a dummy since there is no
    # real auth DB here at all.
    @contextlib.contextmanager
    def _no_auth_conn():
        yield None

    monkeypatch.setattr(db, "auth_conn", _no_auth_conn)
    return store


def test_login_page_is_html(app):
    client = TestClient(app)
    r = client.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text and "user_id" in r.text


def test_successful_login_sets_cookie(app, fake_user):
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert session_mod.SESSION_COOKIE_NAME in r.cookies


def test_wrong_password_is_401(app, fake_user):
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "12345", "password": "wrong"},
    )
    assert r.status_code == 401


def test_unknown_user_is_404(app, fake_user):
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "999", "password": "anything"},
    )
    assert r.status_code == 404


def test_rate_limit_after_5_failures(app, fake_user):
    client = TestClient(app)
    for _ in range(5):
        client.post("/login", data={"user_id": "12345", "password": "x"})
    r = client.post("/login", data={"user_id": "12345", "password": "x"})
    assert r.status_code == 429


def test_logout_clears_cookie(app, fake_user):
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert any(
        session_mod.SESSION_COOKIE_NAME in v
        for v in r.headers.get_list("set-cookie")
    )


def test_session_cookie_round_trip(app, fake_user):
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"user_id": 12345}


def _session_cookie_morsels(resp) -> list[Morsel]:
    """Every session-cookie Morsel from a response's Set-Cookie headers."""
    headers = resp.headers
    # httpx Headers spell it get_list; starlette's spell it getlist.
    get_list = getattr(headers, "get_list", None) or headers.getlist
    morsels = []
    for header in get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        morsel = jar.get(session_mod.SESSION_COOKIE_NAME)
        if morsel is not None:
            morsels.append(morsel)
    if not morsels:
        raise AssertionError("no session cookie in Set-Cookie headers")
    return morsels


def _session_cookie_morsel(resp) -> Morsel:
    """The session-cookie Morsel from a response's Set-Cookie headers."""
    return _session_cookie_morsels(resp)[0]


def test_login_cookie_domain_contract(app, fake_user, monkeypatch):
    """The cookie contract: the shared domain rides the login cookie iff
    SESSION_COOKIE_DOMAIN is set.

    Pinned in BOTH directions: unset must mean host-only (exactly the
    pre-sharing behaviour — a dev checkout is not served from the shared
    domain), set must mean the value lands on the setter. A one-direction
    check passes while the other leg is broken.
    """
    monkeypatch.delenv("SESSION_COOKIE_DOMAIN", raising=False)
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    morsel = _session_cookie_morsel(r)
    assert morsel["domain"] == ""
    # The rest of the flag contract, pinned at the same site.
    assert morsel["path"] == "/"
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "strict"
    assert morsel["max-age"] == str(session_mod.SESSION_COOKIE_MAX_AGE)
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "example.test")
    r = client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    assert _session_cookie_morsel(r)["domain"] == "example.test"


def test_clear_cookie_path_matches_the_setter(monkeypatch):
    """Setter and clearer must agree on path AND domain, or logout breaks.

    A browser keys a cookie on (name, domain, path): a deletion whose path
    or domain differs from the setter's writes an expired cookie that does
    not match the live one, and the real cookie survives the logout.
    """
    # Genuinely unset, not "unset unless the ambient environment sets it":
    # an exported SESSION_COOKIE_DOMAIN would silently turn the host-only
    # leg into a second domain leg.
    monkeypatch.delenv("SESSION_COOKIE_DOMAIN", raising=False)
    setter = Response()
    session_mod.set_session_cookie(setter, "token")
    clearer = Response()
    session_mod.clear_session_cookie(clearer)
    set_morsel = _session_cookie_morsel(setter)
    clear_morsel = _session_cookie_morsel(clearer)
    assert clear_morsel["path"] == set_morsel["path"]
    assert clear_morsel["domain"] == set_morsel["domain"]
    # The deletion must actually expire the cookie, not re-issue it.
    assert clear_morsel["max-age"] == "0"
    assert clear_morsel.value == ""
    # With the shared domain configured, BOTH helpers must carry it —
    # symmetry on the host-only leg alone would not catch a one-sided
    # domain (the exact production break: logout silently stops working).
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "example.test")
    setter = Response()
    session_mod.set_session_cookie(setter, "token")
    clearer = Response()
    session_mod.clear_session_cookie(clearer)
    assert _session_cookie_morsel(setter)["domain"] == "example.test"
    # The clearer emits BOTH deletions — domain-keyed and host-only — and
    # their header order is an implementation detail: assert the multiset
    # of domains, not the first header. A first-header check fails on a
    # behaviourally inert swap of the two delete_cookie calls, with a
    # message pointing at a domain defect that does not exist.
    domains = sorted(m["domain"] for m in _session_cookie_morsels(clearer))
    assert domains == ["", "example.test"], (
        "clear_session_cookie must emit both deletions — host-only and "
        f"domain-keyed — in any order; got domains {domains}"
    )


def test_logout_rejects_the_next_request(app, fake_user):
    """End to end: after logout the session must stop authenticating.

    Helper symmetry (test_clear_cookie_path_matches_the_setter) does not
    pin this: a clear_session_cookie that re-issues a LIVE cookie leaves
    that test green while logout silently stops working. The jar
    assertion is what catches that mutant — it stores the live cookie as
    the non-empty value '""', so the server-side 401 alone cannot tell
    it apart from a real deletion.
    """
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    r = client.get("/api/me")
    assert r.status_code == 200
    client.get("/logout", follow_redirects=False)
    # The jar must not hold a usable session cookie after logout.
    assert not client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    # Unauthenticated /api/* gets a JSON 401 (see auth_middleware).
    r = client.get("/api/me")
    assert r.status_code == 401


def test_logout_clears_a_pre_rollout_host_only_cookie(app, fake_user, monkeypatch):
    """The rollout boundary, not the steady state.

    A user logged in BEFORE SESSION_COOKIE_DOMAIN was turned on holds a
    host-only session cookie; their next login after the rollout adds a
    domain-keyed one — a DIFFERENT (name, domain, path) key, so the jar
    holds both. A logout that clears only one keying leaves the other
    alive and still authenticating (/logout does no server-side
    revocation, so nothing else catches it). The post-logout request is
    the assertion that matters: header inspection cannot see a surviving
    cookie that still resolves.
    """
    # Pre-rollout: no domain configured, login issues a host-only cookie.
    monkeypatch.delenv("SESSION_COOKIE_DOMAIN", raising=False)
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    assert client.get("/api/me").status_code == 200
    # The rollout happens between the two logins. The domain needs an
    # embedded dot, re-derived against THIS test client rather than
    # copied: the client's request host is testserver.local, and a
    # dotless Domain=testserver cookie is stored by http.cookiejar but
    # never returned to that host — only a dotted domain makes the
    # two-cookie state materialise (verified with a probe script against
    # this repo's fastapi TestClient + httpx + http.cookiejar versions).
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "testserver.local")
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    # Precondition: the jar really holds BOTH keyings — one host-only
    # (bare domain), one domain-keyed (leading dot) — or the rest of the
    # test proves nothing.
    jar_domains = sorted(
        c.domain
        for c in client.cookies.jar
        if c.name == session_mod.SESSION_COOKIE_NAME
    )
    assert len(jar_domains) == 2, f"expected two cookie keyings, got {jar_domains}"
    assert any(d.startswith(".") for d in jar_domains)
    assert any(not d.startswith(".") for d in jar_domains)
    client.get("/logout", follow_redirects=False)
    # Neither surviving cookie may authenticate — this is the assertion
    # that fails when EITHER deletion leg is dropped.
    r = client.get("/api/me")
    assert r.status_code == 401
    assert not client.cookies.get(session_mod.SESSION_COOKIE_NAME)


def test_guest_logout_with_domain_set_rejects_the_next_request(app, monkeypatch):
    """Guest logout must work when SESSION_COOKIE_DOMAIN is set.

    Guest cookies are host-only BY DESIGN (the guest secret is
    per-service — see set_session_cookie), so when the variable is set
    the domain-keyed deletion can never match a guest cookie: a clearer
    that deletes only the domain keying leaves the guest cookie alive
    and still authenticating. The post-logout request is the assertion
    that matters — header inspection cannot see a surviving cookie that
    still resolves.
    """
    # Same dotted-domain rule as the pre-rollout test above: a dotless
    # Domain=testserver cookie is never returned to this client's host.
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "testserver.local")
    client = TestClient(app)
    client.post("/login/guest", follow_redirects=False)
    assert client.get("/api/me").status_code == 200
    client.get("/logout", follow_redirects=False)
    # The surviving host-only guest cookie must NOT authenticate — this
    # is the assertion that fails when the host-only deletion is dropped.
    r = client.get("/api/me")
    assert r.status_code == 401
    assert not client.cookies.get(session_mod.SESSION_COOKIE_NAME)


def test_guest_cookie_gets_no_domain(app, monkeypatch):
    """A guest cookie must stay host-only even when the domain is set.

    The guest secret is per-service: a guest cookie issued with the
    shared domain would be rejected by every service except the one that
    minted it, and would shadow the host-only guest cookie there.
    """
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "example.test")
    client = TestClient(app)
    r = client.post("/login/guest", follow_redirects=False)
    assert r.status_code in (302, 303)
    morsel = _session_cookie_morsel(r)
    assert morsel["domain"] == ""


def _helper_body_spans(src: str) -> list[tuple[int, int]]:
    """1-based line spans of the two cookie helpers, for the guard's exemption.

    Spans come from `ast` — lineno..end_lineno of the top-level def — not
    text scanning: a search for the next "def " cannot see `async def`,
    so the last plain-def helper's span would run to end of file and
    silently exempt the tail of session.py.
    """
    spans = []
    for node in ast.parse(src).body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ("set_session_cookie", "clear_session_cookie")
        ):
            spans.append((node.lineno, node.end_lineno))
    return spans


def test_all_cookie_sites_use_the_helper():
    """Flags session-cookie set/delete calls outside the two helpers.

    What it actually checks: `.set_cookie(` and `.delete_cookie(` matches
    in backend/*.py whose following 400 characters mention
    SESSION_COOKIE_NAME, exempting matches on a line inside the
    ast-computed body span of set_session_cookie / clear_session_cookie
    (the two legitimate sites). A span that reaches the last line of the
    file is a bug, not an exemption — the guard fails loudly rather than
    silently exempting the tail.

    Unlike the claudit original this is ported from, the file list is
    NOT hardcoded: every backend module is scanned, so a new module with
    a stray cookie call cannot slip past a stale list. The scan-count
    assertion keeps the glob itself honest — a guard that passes because
    it inspects no files is worse than no guard. The domain attribute is
    set inside both helpers from one shared source; divergent copies are
    how one site silently misses it.

    Known hole, accepted for now: a call written with a literal
    cookie-name string instead of SESSION_COOKIE_NAME is not matched.
    """
    root = Path(__file__).resolve().parents[1]
    offenders = []
    scanned = 0
    for path in sorted((root / "backend").glob("*.py")):
        scanned += 1
        src = path.read_text(encoding="utf-8")
        total_lines = len(src.splitlines())
        exempt = _helper_body_spans(src)
        for start, end in exempt:
            assert end < total_lines, (
                f"{path.name}: helper span {start}-{end} reaches the last "
                "line of the file; the span computation is suspect, so "
                "refusing to exempt the tail"
            )
        for m in re.finditer(r"\.(?:set_cookie|delete_cookie)\(", src):
            line = src[: m.start()].count("\n") + 1
            window = src[m.start(): m.start() + 400]
            inside_helper = any(s <= line <= e for s, e in exempt)
            if "SESSION_COOKIE_NAME" in window and not inside_helper:
                offenders.append(f"{path.name}:{line}")
    assert scanned > 2, "the backend glob matched too few files to be a guard"
    assert not offenders, (
        "session cookie set/cleared outside the session.py helpers: "
        + ", ".join(offenders)
    )

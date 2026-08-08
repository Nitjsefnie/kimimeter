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
    #
    # is_session_active must stay pinned at True even though /logout now
    # revokes server-side: the logout tests below assert a post-logout 401
    # specifically to prove a cookie-DELETION leg fired (header inspection
    # cannot see a surviving cookie that still resolves). If revocation
    # could also produce that 401, those tests would pass with a deletion
    # leg removed and the dual-keying clearing would be untested. So
    # revocation is stubbed to a no-op instead — the real revocation path
    # is pinned against a real auth DB in test_sessions_repo.py.
    monkeypatch.setattr(
        sessions_repo, "is_session_active", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        sessions_repo, "touch_session", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        sessions_repo, "revoke_session", lambda *args, **kwargs: True
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


def _assert_both_deletion_legs(response):
    """Both (name, domain, path) keyings must get a real DELETION header.

    The full key is asserted, not just the domain half: the two legs must
    differ on Domain= (one keyed to the shared domain, one host-only),
    both must carry the same Path=/ the setter uses — a deletion keyed
    (name, domain, /other-path) matches nothing the browser holds — and
    both must actually DELETE (empty value, expiry in the past), not
    re-issue a live cookie.
    """
    morsels = _session_cookie_morsels(response)
    assert any(m["domain"] for m in morsels)
    assert any(not m["domain"] for m in morsels)
    for m in morsels:
        assert m["path"] == "/"
        assert m.value.strip('"') == ""
        assert "1970" in m["expires"] or m["max-age"] == "0"


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
    alive and still authenticating. The post-logout request is the
    assertion that matters: header inspection cannot see a surviving
    cookie that still resolves. Now that /logout also revokes
    server-side, the final 401 has a second possible cause — but not in
    this test: the fake_user fixture stubs revoke_session to a no-op and
    pins is_session_active at True, so here the 401 can still come from
    the cookie path alone and keeps discriminating a dropped deletion
    leg.
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


def test_logout_fails_loudly_when_the_revocation_raises(
    app, fake_user, monkeypatch
):
    """A logout that cannot revoke must not report success.

    Clearing the cookie while the row survives is the exact defect the
    revocation exists to remove: the user is told they signed out, the
    local cookie is gone, and the token stays valid for every sibling
    service with no cookie left to retry the revocation with. So the
    revocation runs BEFORE the response is built, and its failure
    propagates. Both assertions are load-bearing: the 500 kills the
    swallow (a swallowed revocation returns the normal 303), and the
    absent Set-Cookie pins that no cookie deletion is handed out
    alongside the failure.
    """
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    token = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert token

    def _boom(*_args, **_kwargs):
        raise RuntimeError("auth DB unreachable")

    monkeypatch.setattr(sessions_repo, "revoke_session", _boom)

    # raise_server_exceptions=False so the server error surfaces as a
    # response to assert on instead of propagating into the test.
    quiet = TestClient(app, raise_server_exceptions=False)
    quiet.cookies.set(session_mod.SESSION_COOKIE_NAME, token)
    r = quiet.get("/logout", follow_redirects=False)
    assert r.status_code == 500
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_guest_logout_never_touches_the_auth_db(app, monkeypatch):
    """The guest exclusion must hold with the auth DB down — by assertion.

    Guests have no web_sessions rows by design, so logout returns before
    ANY auth-DB access for them, and a total auth-DB outage must not
    break guest logout. Removing the exclusion makes the handler call
    revoke_session, which reaches for the (here: raising) pool and the
    logout 500s — without this test that mutant would be caught only by
    an incidental PoolTimeout from a scratch auth DB that happened not
    to exist, which is a crash, and one that depends on test ordering.

    Status alone cannot pin the guest path: a no-op handler that skips
    clear_session_cookie ALSO returns 303. The clearing assertions are
    what fail there — under a no-op the jar still holds the live guest
    cookie and the next request keeps authenticating.

    Deliberate overlap: the domain-set half of this test re-covers
    test_guest_logout_with_domain_set_rejects_the_next_request. Both are
    kept — this one pins zero auth-DB access BY ASSERTION (the other
    would catch a removed guest exclusion only via an incidental
    PoolTimeout crash, dependent on test ordering), while that one runs
    with no db.auth_conn monkeypatch at all, so it alone sees a
    regression in the interplay between the guest path and the real
    pool.
    """
    def _down():
        raise RuntimeError("auth DB down")

    monkeypatch.setattr(db, "auth_conn", _down)
    # Domain set: also pins that guest logout clears BOTH keyings while
    # the guest cookie itself is host-only by design.
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "testserver.local")
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/login/guest", follow_redirects=False)
    # Precondition: the guest cookie really authenticates, or a later
    # 401 proves nothing.
    assert client.get("/api/me").status_code == 200
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    # The guest path must still CLEAR the cookie — the assertions a bare
    # 303-redirect handler fails.
    _assert_both_deletion_legs(r)
    assert not client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert client.get("/api/me").status_code == 401


def test_logout_without_a_cookie_still_clears_both_keyings(app, monkeypatch):
    """No cookie at all: the same 303 with both deletion legs, never a 500.

    Nothing exercised this leg — an unguarded index into a None parse
    result would 500 here while every existing logout test stayed green.
    """
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "example.test")
    client = TestClient(app)
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    _assert_both_deletion_legs(r)


def test_logout_with_a_malformed_cookie_still_clears_both_keyings(
    app, monkeypatch
):
    """A cookie that does not parse: the same 303 with both deletion legs.

    parse_session_token returns None for garbage; the handler must skip
    the revocation quietly and still clear, or a corrupted cookie would
    make logout 500 — and nothing checked that.
    """
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "example.test")
    client = TestClient(app)
    client.cookies.set(session_mod.SESSION_COOKIE_NAME, "not.a.real.token")
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    _assert_both_deletion_legs(r)


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

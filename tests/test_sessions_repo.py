"""web_sessions repository — record, lookup, revoke — plus the end-to-end
revocation path through the real app.

Uses the shared `auth_db` fixture from tests/conftest.py. The imports are
top-level: pytest loads conftest.py — and therefore the test DSN
setdefaults — before this module body runs. Nonces use the km- prefix so
this suite can never collide with a sibling service's suite if both are
ever pointed at one scratch database.
"""
import json
from http.cookies import SimpleCookie

from fastapi.testclient import TestClient

from backend import auth, db, sessions_repo
from backend import session as session_mod
from backend.app import app as real_app


def test_record_then_active(auth_db):
    sessions_repo.record_session(7, "km-nonce-a", "curl/8", "10.0.0.1")
    assert sessions_repo.is_session_active("km-nonce-a") is True


def test_unknown_nonce_is_not_active(auth_db):
    assert sessions_repo.is_session_active("km-never-issued") is False


def test_revoked_nonce_is_not_active(auth_db):
    sessions_repo.record_session(7, "km-nonce-b", "curl/8", "10.0.0.1")
    assert sessions_repo.revoke_session(7, "km-nonce-b") is True
    assert sessions_repo.is_session_active("km-nonce-b") is False


def test_revoke_only_affects_own_user(auth_db):
    sessions_repo.record_session(7, "km-nonce-c", "curl/8", "10.0.0.1")
    assert sessions_repo.revoke_session(8, "km-nonce-c") is False
    assert sessions_repo.is_session_active("km-nonce-c") is True


def test_revoked_session_stops_authenticating(logged_in_client):
    """End to end: a session revoked in the auth DB must stop
    authenticating through the real request path, not just at the repo
    layer. Revokes with raw SQL so the wiring — not revoke_session — is
    what the assertions pin."""
    client = logged_in_client
    resp = client.get("/api/me")
    assert resp.status_code == 200
    assert resp.json()["user_id"] != session_mod.GUEST_USER_ID

    cookie = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    parsed = session_mod.parse_session_token(cookie)
    assert parsed is not None
    with db.auth_conn() as c:
        c.execute(
            "UPDATE web_sessions SET revoked_at = now() WHERE nonce = %s",
            (parsed[2],),
        )

    resp = client.get("/api/me")
    assert resp.status_code == 401


def _seed_auth_user(user_id: int, secret: str, password: str) -> None:
    """Insert a private user row into the scratch auth DB (auth_db
    fixture), with a web password and a session secret, so a real login
    and real token minting work against it."""
    config = {session_mod.WEB_SESSION_SECRET_KEY: secret}
    auth.set_web_password(config, password)
    with db.auth_conn() as c:
        c.execute(
            "INSERT INTO users (user_id, config) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (user_id) DO UPDATE SET config = EXCLUDED.config",
            (user_id, json.dumps(config)),
        )


def test_logout_revokes_the_session_row(auth_db):
    """The acceptance pin: after logout, the same token presented by a
    client that never saw the deletion response must be rejected.

    Asserting the logout response's status, redirect, or Set-Cookie
    headers passes in full while the session stays alive server-side —
    cookie clearing is invisible to a second client holding the same
    token, which stands in for a different service in the fleet. Only a
    server-side revocation of the token's own nonce can make its /api/me
    fail.

    This test lives in THIS file, not tests/test_login.py: that module's
    fake_user fixture stubs is_session_active to True unconditionally
    and revoke_session to a no-op, so a pin written against those
    fixtures could never fail when the revocation does nothing. Here the
    real app and the scratch auth DB make the final 401 attributable to
    the revoked row — the no-op-revocation mutants (the handler's
    revoke_session call removed, or revoke_session itself returning
    without writing) fail it by assertion.
    """
    # Private user_id: 7 and 8 are taken by the repo-layer tests above
    # and the shared fixture user 4242 by logged_in_client, all on this
    # module-scoped database.
    uid = 987010
    _seed_auth_user(uid, "task7a-acceptance-session-secret", "task7a-pw")

    client = TestClient(real_app)
    resp = client.post(
        "/login",
        data={"user_id": str(uid), "password": "task7a-pw"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    # Capture BEFORE the logout — the logout empties this client's jar.
    token = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert token
    parsed = session_mod.parse_session_token(token)
    assert parsed is not None

    # A second client holding the same token, which the logout's deletion
    # response can never reach — a sibling service in the fleet.
    other = TestClient(real_app)
    other.cookies.set(session_mod.SESSION_COOKIE_NAME, token)
    # Precondition: the token really authenticates, or a later 401 proves
    # nothing — the token could never have worked at all.
    assert other.get("/api/me").status_code == 200

    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303

    # The second client's jar still holds the identical token — the 401
    # below cannot be a deletion that somehow propagated, only the
    # server-side revocation. (Iterate rather than .get(): a jar holding
    # the same name under two keyings makes .get() raise CookieConflict.)
    jar_values = [
        c.value
        for c in other.cookies.jar
        if c.name == session_mod.SESSION_COOKIE_NAME
    ]
    assert jar_values and all(v == token for v in jar_values)
    assert other.get("/api/me").status_code == 401
    # Unit-level pin, localising a failure to "the row was not revoked"
    # versus "the middleware did not reject".
    assert sessions_repo.is_session_active(parsed[2]) is False


def test_logout_with_forged_cookie_cannot_revoke_a_victims_session(auth_db):
    """The revoked user_id must come from the VERIFIED resolution, never
    from parse_session_token.

    parse_session_token performs no signature check — every field it
    returns is attacker-controlled. /logout is on the public-path
    allowlist, so an attacker who learns a victim's nonce can forge
    <victim_uid>.<any_ts>.<victim_nonce>.garbage and hit /logout. A
    handler that revokes with the parsed (unverified) user_id kills the
    victim's live session across the whole fleet — revoke_session's
    AND user_id = %s filter cannot stop it, because the attacker supplied
    the matching user_id. The victim's session must survive.
    """
    # Private user_id: 7, 8, 4242 and 987010 are taken.
    victim_uid = 987011
    victim_secret = "task7a-victim-session-secret"
    _seed_auth_user(victim_uid, victim_secret, "task7a-victim-pw")

    victim_token = session_mod.make_session_token(victim_uid, victim_secret)
    parsed = session_mod.parse_session_token(victim_token)
    assert parsed is not None
    nonce = parsed[2]
    sessions_repo.record_session(victim_uid, nonce, "curl", "127.0.0.1")
    # Precondition: the victim's session is live, so its survival means
    # the forged logout was refused — not that there was nothing to kill.
    assert session_mod.resolve_session_user_id(victim_token) == victim_uid

    forged = f"{victim_uid}.{parsed[1]}.{nonce}.{'0' * 64}"
    attacker = TestClient(real_app)
    attacker.cookies.set(session_mod.SESSION_COOKIE_NAME, forged)
    r = attacker.get("/logout", follow_redirects=False)
    # A forged cookie is not an error: logout still succeeds locally.
    assert r.status_code == 303
    # ...but it must not have revoked the victim's row.
    assert sessions_repo.is_session_active(nonce) is True
    assert session_mod.resolve_session_user_id(victim_token) == victim_uid


def test_logout_revokes_only_the_presented_session(auth_db):
    """Logging out revokes THIS session, never the user's other sessions.

    A blanket revoke keyed on user_id alone signs the user out on every
    other device — and was measured to leave the whole suite green on
    the sibling service this shape is the template for. Two logins, two
    nonces: logging out of the first must revoke exactly it, and the
    second must stay active and keep authenticating.
    """
    # Private user_id: 7, 8, 4242, 987010 and 987011 are taken.
    uid = 987012
    _seed_auth_user(uid, "task7a-second-device-secret", "task7a-devices-pw")

    client1 = TestClient(real_app)
    r1 = client1.post(
        "/login",
        data={"user_id": str(uid), "password": "task7a-devices-pw"},
        follow_redirects=False,
    )
    assert r1.status_code == 303, r1.text
    token1 = client1.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert token1

    # A second device: its own login, its own nonce.
    client2 = TestClient(real_app)
    r2 = client2.post(
        "/login",
        data={"user_id": str(uid), "password": "task7a-devices-pw"},
        follow_redirects=False,
    )
    assert r2.status_code == 303, r2.text
    token2 = client2.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert token2

    parsed1 = session_mod.parse_session_token(token1)
    parsed2 = session_mod.parse_session_token(token2)
    assert parsed1 is not None and parsed2 is not None
    assert parsed1[2] != parsed2[2]
    # Precondition: both sessions are live, so the second's survival
    # means the revoke was scoped — not that there was nothing to kill.
    assert sessions_repo.is_session_active(parsed1[2]) is True
    assert sessions_repo.is_session_active(parsed2[2]) is True

    r = client1.get("/logout", follow_redirects=False)
    assert r.status_code == 303

    assert sessions_repo.is_session_active(parsed1[2]) is False
    # The assertions the blanket UPDATE ... WHERE user_id mutant fails.
    assert sessions_repo.is_session_active(parsed2[2]) is True
    assert client2.get("/api/me").status_code == 200


def test_two_nonce_rollout_boundary_logout_leaves_the_other_session_live(
    auth_db,
):
    """The authenticated witness for the surviving-cookie rationale.

    The rollout boundary can leave a browser holding two same-named
    cookies under two keyings (host-only from before SESSION_COOKIE_DOMAIN
    was turned on, domain-keyed after) that name TWO DIFFERENT live
    sessions. Logout revokes the presented session and leaves the other
    live one in place (pinned by
    test_logout_revokes_only_the_presented_session), so the other nonce
    survives and its cookie keeps authenticating — one of the two
    witnesses clear_session_cookie's docstring names for its claim that
    a surviving host-only cookie CAN still authenticate, and the
    disproof of the earlier "holds only for guest sessions" qualifier.

    Which nonce is presented is deterministic here: Starlette parses the
    Cookie header with its own cookie_parser (starlette/requests.py —
    explicitly NOT SimpleCookie, per the comment in its source), which
    assigns each ;-separated pair into a dict, so a duplicated name
    resolves to its LAST occurrence. token2 (the post-rollout login) is
    therefore what the handler resolves and revokes. token1 plays the
    surviving pre-rollout cookie; a client that never saw the deletion
    response stands in for the browser still holding it.
    """
    # Private user_id: 7, 8, 4242 and 987010-987012 are taken.
    uid = 987013
    _seed_auth_user(uid, "task7a-rollout-secret", "task7a-rollout-pw")

    # Two real logins — the pre-rollout and post-rollout sign-ins.
    def _login() -> str:
        client = TestClient(real_app)
        r = client.post(
            "/login",
            data={"user_id": str(uid), "password": "task7a-rollout-pw"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        token = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
        assert token
        return token

    token1 = _login()
    token2 = _login()

    parsed1 = session_mod.parse_session_token(token1)
    parsed2 = session_mod.parse_session_token(token2)
    assert parsed1 is not None and parsed2 is not None
    # Two distinct live sessions — without this the test could pass
    # because one session was never valid.
    assert parsed1[2] != parsed2[2]
    assert sessions_repo.is_session_active(parsed1[2]) is True
    assert sessions_repo.is_session_active(parsed2[2]) is True

    # ONE logout carrying BOTH cookies, as the rollout boundary produces.
    # Fresh client: empty jar, so the explicit header is the only Cookie.
    boundary = TestClient(real_app)
    r = boundary.get(
        "/logout",
        headers={
            "Cookie": (
                f"{session_mod.SESSION_COOKIE_NAME}={token1}; "
                f"{session_mod.SESSION_COOKIE_NAME}={token2}"
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    # The presented nonce is revoked; the other survives and still
    # authenticates — the property the docstring may claim.
    assert sessions_repo.is_session_active(parsed2[2]) is False
    assert sessions_repo.is_session_active(parsed1[2]) is True
    holder = TestClient(real_app)
    holder.cookies.set(session_mod.SESSION_COOKIE_NAME, token1)
    assert holder.get("/api/me").status_code == 200


def test_guest_cookie_surviving_a_boundary_logout_still_authenticates(
    auth_db, monkeypatch
):
    """The guest witness for the surviving-cookie rationale — and the
    regression guard for the wording itself.

    clear_session_cookie's docstring claims a surviving host-only
    cookie CAN still authenticate and names two witnesses; this is the
    guest one. A guest token's resolution consults no web_sessions row
    (nothing ever records one for a guest), so a boundary logout that
    revokes the presented authenticated session has nothing it could
    revoke for the guest, and the surviving guest cookie keeps
    authenticating. The pair this test asserts — is_session_active
    False AND /api/me 200 — is the measured counterexample that broke
    the earlier "liveness" wording: by the row predicate this cookie is
    not live, yet it authenticates.

    The deletion-header assertions are the conditional-deletion
    mutant's kill: with the domain set, a host-only deletion made
    conditional on the domain being unset would never reach this
    cookie's keying.
    """
    # Domain set, so the host-only deletion leg is the one that can
    # reach the guest cookie (guest cookies are host-only by design).
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "testserver.local")
    # Private user_id: 7, 8, 4242 and 987010-987015 are taken.
    uid = 987016
    _seed_auth_user(uid, "task7a-guestwit-secret", "task7a-guestwit-pw")

    # The guest cookie: a real guest login. It stays host-only even
    # with the domain set, so the TestClient jar holds it.
    guest = TestClient(real_app)
    r = guest.post("/login/guest", follow_redirects=False)
    assert r.status_code == 303, r.text
    guest_token = guest.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert guest_token
    parsed_guest = session_mod.parse_session_token(guest_token)
    assert parsed_guest is not None
    assert parsed_guest[0] == session_mod.GUEST_USER_ID
    # The witness pair, before the logout: NO row (nothing a logout
    # could revoke) — and the cookie authenticates.
    assert sessions_repo.is_session_active(parsed_guest[2]) is False
    assert guest.get("/api/me").status_code == 200

    # The presented session: a real authenticated login. With the
    # domain set the jar refuses the Domain=testserver.local cookie for
    # host testserver, so the token comes from the response header.
    client = TestClient(real_app)
    r = client.post(
        "/login",
        data={"user_id": str(uid), "password": "task7a-guestwit-pw"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    jar = SimpleCookie()
    for header in r.headers.get_list("set-cookie"):
        jar.load(header)
    morsel = jar.get(session_mod.SESSION_COOKIE_NAME)
    assert morsel is not None
    presented = morsel.value
    parsed_presented = session_mod.parse_session_token(presented)
    assert parsed_presented is not None
    assert sessions_repo.is_session_active(parsed_presented[2]) is True

    # ONE boundary logout carrying BOTH cookies; Starlette's
    # cookie_parser resolves a duplicated name to its LAST occurrence,
    # so `presented` is what the handler resolves and revokes and the
    # guest token plays the surviving pre-rollout cookie. A fresh
    # client keeps the explicit header as the one Cookie.
    boundary = TestClient(real_app)
    r = boundary.get(
        "/logout",
        headers={
            "Cookie": (
                f"{session_mod.SESSION_COOKIE_NAME}={guest_token}; "
                f"{session_mod.SESSION_COOKIE_NAME}={presented}"
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert sessions_repo.is_session_active(parsed_presented[2]) is False

    # The logout clears BOTH keyings: the domain-keyed one and,
    # unconditionally, the host-only one — the leg a conditional
    # deletion drops, abandoning the guest cookie at logout.
    deletions = [
        h
        for h in r.headers.get_list("set-cookie")
        if h.lower().startswith(f"{session_mod.SESSION_COOKIE_NAME}=")
    ]
    assert any("domain=testserver.local" in h.lower() for h in deletions)
    assert any("domain=" not in h.lower() for h in deletions)

    # The witness pair, after the logout: still no row — and the
    # surviving guest cookie STILL authenticates.
    assert sessions_repo.is_session_active(parsed_guest[2]) is False
    assert guest.get("/api/me").status_code == 200


def test_surviving_cookie_with_an_unrecorded_nonce_does_not_authenticate(
    auth_db,
):
    """A surviving cookie whose nonce was never recorded: this test's
    own case, measured end to end.

    The surviving cookie is validly signed (same user, same secret as
    the presented session) but its nonce has no web_sessions row and
    never did; is_session_active on it is False before the logout. The
    boundary logout revokes the OTHER (presented) session, and
    afterwards /api/me carrying this cookie returns 401. One of the
    two measured counterexamples to the earlier "ceases to hold only
    when the surviving cookie carries the same nonce" wording: the
    orphan nonce is a DIFFERENT nonce from the one the logout just
    revoked, and it still fails.
    """
    # Private user_id: 7, 8, 4242 and 987010-987013 are taken.
    uid = 987014
    secret = "task7a-unrecorded-secret"
    _seed_auth_user(uid, secret, "task7a-unrecorded-pw")

    # The presented session: a real login, so a real web_sessions row.
    client = TestClient(real_app)
    r = client.post(
        "/login",
        data={"user_id": str(uid), "password": "task7a-unrecorded-pw"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    presented = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert presented
    parsed_presented = session_mod.parse_session_token(presented)
    assert parsed_presented is not None
    assert sessions_repo.is_session_active(parsed_presented[2]) is True

    # The surviving cookie: VALIDLY SIGNED (same user, same secret) but
    # its nonce has no web_sessions row and never did.
    orphan = session_mod.make_session_token(uid, secret)
    parsed_orphan = session_mod.parse_session_token(orphan)
    assert parsed_orphan is not None
    assert parsed_orphan[2] != parsed_presented[2]
    # Precondition: signature-valid but NOT live. This liveness
    # assertion is what attributes the post-logout 401 below to the
    # missing row rather than to the logout — a pre-logout /api/me
    # assertion could never fail independently of the final one (the
    # boundary logout touches the presented session's row, and the
    # orphan has no row a revocation could change), so it is folded
    # into this one.
    assert sessions_repo.is_session_active(parsed_orphan[2]) is False

    # ONE logout carrying BOTH cookies, as the rollout boundary produces.
    # Starlette's cookie_parser resolves a duplicated name to its LAST
    # occurrence, so `presented` is what the handler resolves and revokes.
    boundary = TestClient(real_app)
    r = boundary.get(
        "/logout",
        headers={
            "Cookie": (
                f"{session_mod.SESSION_COOKIE_NAME}={orphan}; "
                f"{session_mod.SESSION_COOKIE_NAME}={presented}"
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert sessions_repo.is_session_active(parsed_presented[2]) is False

    # The surviving cookie names a DIFFERENT nonce than the one just
    # revoked — and still does not authenticate, because its session
    # has no web_sessions row (asserted above), which the boundary
    # logout cannot change.
    holder = TestClient(real_app)
    holder.cookies.set(session_mod.SESSION_COOKIE_NAME, orphan)
    assert holder.get("/api/me").status_code == 401


def test_surviving_cookie_revoked_earlier_does_not_authenticate(auth_db):
    """A surviving cookie revoked EARLIER by another path, not this
    logout: this test's own case.

    The second measured counterexample to the "same nonce" wording. Two
    real logins, two live sessions; the first is revoked BEFORE the
    boundary logout, via sessions_repo.revoke_session directly — a
    revocation by another path than the logout below (going through the
    HTTP layer would add only cookie plumbing). The surviving cookie
    then names a session that is not live, and this test asserts that
    it does NOT authenticate after the logout — even though its nonce
    is DIFFERENT from the one this logout revoked.
    """
    # Private user_id: 7, 8, 4242 and 987010-987014 are taken.
    uid = 987015
    _seed_auth_user(uid, "task7a-earlier-secret", "task7a-earlier-pw")

    def _login() -> str:
        client = TestClient(real_app)
        r = client.post(
            "/login",
            data={"user_id": str(uid), "password": "task7a-earlier-pw"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        token = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
        assert token
        return token

    earlier = _login()
    presented = _login()
    parsed_earlier = session_mod.parse_session_token(earlier)
    parsed_presented = session_mod.parse_session_token(presented)
    assert parsed_earlier is not None and parsed_presented is not None
    assert parsed_earlier[2] != parsed_presented[2]
    assert sessions_repo.is_session_active(parsed_earlier[2]) is True
    assert sessions_repo.is_session_active(parsed_presented[2]) is True

    holder = TestClient(real_app)
    holder.cookies.set(session_mod.SESSION_COOKIE_NAME, earlier)
    # Precondition: the surviving cookie really authenticates before the
    # earlier revocation, or the final 401 proves nothing.
    assert holder.get("/api/me").status_code == 200

    # The EARLIER revocation, by another path than the logout below.
    assert sessions_repo.revoke_session(uid, parsed_earlier[2]) is True
    assert sessions_repo.is_session_active(parsed_earlier[2]) is False
    assert sessions_repo.is_session_active(parsed_presented[2]) is True

    # The boundary logout revokes the OTHER (presented) session.
    boundary = TestClient(real_app)
    r = boundary.get(
        "/logout",
        headers={
            "Cookie": (
                f"{session_mod.SESSION_COOKIE_NAME}={earlier}; "
                f"{session_mod.SESSION_COOKIE_NAME}={presented}"
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert sessions_repo.is_session_active(parsed_presented[2]) is False

    # The surviving cookie names a different, EARLIER-REVOKED session —
    # the case this test measures: it does not authenticate.
    assert holder.get("/api/me").status_code == 401

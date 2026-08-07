import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest

from starlette.requests import Request

from backend import db
from backend import session
from backend import sessions_repo


def test_token_roundtrip():
    secret = "super-secret-32-bytes" * 2
    tok = session.make_session_token(99, secret)
    assert session.verify_session_token(tok, secret) == 99


def test_verify_rejects_wrong_secret():
    tok = session.make_session_token(42, "secret-a" * 4)
    assert session.verify_session_token(tok, "secret-b" * 4) is None


def test_resolve_accepts_token_older_than_max_age(seeded_user):
    """Sessions end by revocation, not by the clock: a token issued far
    longer ago than SESSION_COOKIE_MAX_AGE still resolves to its user
    while its nonce has an active web_sessions row.

    The age is expressed relative to the constant, never hardcoded — an
    offset that happens to sit below the constant would pass even with
    the old clock-expiry check in place, proving nothing. The decade-scale
    offset is the point: a backdate of one day past the constant only
    proves the window is at least eight days, so a quiet replacement
    expiry of 30 days (or a year) would still pass. No plausible expiry
    tolerates a decade-old token, so "the clock became more generous"
    cannot explain a pass. The nonce is recorded AND asserted active, so
    resolve can only succeed by passing signature verification — it
    cannot pass for the wrong reason."""
    uid, secret = seeded_user
    issued = int(time.time()) - (
        session.SESSION_COOKIE_MAX_AGE + 10 * 365 * 86400
    )
    with patch.object(session.time, "time", return_value=issued):
        tok = session.make_session_token(uid, secret)
    parsed = session.parse_session_token(tok)
    assert parsed is not None
    sessions_repo.record_session(uid, parsed[2], "curl", "127.0.0.1")
    # Precondition: the nonce really is active, so resolve can only
    # succeed through the verification path.
    assert sessions_repo.is_session_active(parsed[2]) is True
    assert session.resolve_session_user_id(tok) == uid


def test_verify_rejects_future_token():
    secret = "k" * 32
    payload = "5.99999999999.nonce"
    sig = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    tok = f"{payload}.{sig}"
    assert session.verify_session_token(tok, secret) is None


def test_parse_session_token_rejects_garbage():
    assert session.parse_session_token("not.a.real.token.too.many") is None
    assert session.parse_session_token("missing-dots") is None
    assert session.parse_session_token("a.b.c.d") is None


def test_get_or_create_session_secret_persists():
    config: dict = {}
    s1 = session.get_or_create_session_secret(config)
    assert config[session.WEB_SESSION_SECRET_KEY] == s1
    s2 = session.get_or_create_session_secret(config)
    assert s2 == s1


def test_get_or_create_session_secret_mints_over_non_string():
    # A stored non-string is absent, so a fresh secret is minted and
    # persisted in its place — never a blank one.
    config = {session.WEB_SESSION_SECRET_KEY: None}
    secret = session.get_or_create_session_secret(config)
    assert secret
    assert config[session.WEB_SESSION_SECRET_KEY] == secret


def test_check_origin_allows_safe_methods():
    scope = {
        "type": "http", "method": "GET", "headers": [],
        "path": "/api/projects",
    }
    req = Request(scope)
    assert session.check_origin(req)


def test_check_origin_rejects_cross_origin_post():
    scope = {
        "type": "http", "method": "POST",
        "headers": [
            (b"host", b"viz.example.com"),
            (b"origin", b"https://evil.example.com"),
        ],
        "path": "/admin/ingest",
    }
    req = Request(scope)
    assert not session.check_origin(req)


def test_check_origin_accepts_same_origin_post():
    scope = {
        "type": "http", "method": "POST",
        "headers": [
            (b"host", b"viz.example.com"),
            (b"origin", b"https://viz.example.com"),
        ],
        "path": "/admin/ingest",
    }
    req = Request(scope)
    assert session.check_origin(req)


@pytest.mark.parametrize(
    "stored",
    # The big int is load-bearing but only RAISES the sampled bar: every
    # other value has a short str(), on which a "len(str(value)) > N"
    # length floor is indistinguishable from the isinstance check, and a
    # 20-char str() kills only a floor set below 20 (e.g. > 5). A
    # disjunction "isinstance(...) or len(str(value)) > N" with N above
    # 20 still survives this list — a sample cannot close an unbounded
    # input class. The property test below
    # (test_non_string_secret_rejected_at_every_str_length) bounds it:
    # proven up to the ladder's reach, survivable above it.
    [None, 0, False, [], {}, 12345678901234567890],
    ids=["null", "zero", "false", "empty-list", "empty-dict", "long-int"],
)
def test_non_string_session_secret_is_treated_as_absent(auth_db, stored):
    """A stored non-string secret is ABSENT, never coerced with str().

    The account must fail closed: a token signed with str(stored).strip()
    — the "None" / "0" / "False" / "[]" / "{}" a str() coercion would
    silently adopt as the shared secret for every such account — must
    not resolve. The forged nonce is recorded AND asserted active, so
    the web_sessions check cannot mask the coercion: only the secret
    handling keeps this token out. Nonces are disjoint across cases by
    construction (make_session_token draws a fresh random nonce)."""
    uid = 4321
    # The binding assertion targets the helper's isinstance contract
    # directly, not the public resolve path below.
    # pylint: disable=protected-access
    assert session._stored_session_secret(
        {session.WEB_SESSION_SECRET_KEY: stored}
    ) == ""
    assert session._stored_session_secret({session.WEB_SESSION_SECRET_KEY: "s"}) == "s"
    with db.auth_conn() as c:
        c.execute(
            "INSERT INTO users (user_id, config) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (user_id) DO UPDATE SET config = EXCLUDED.config",
            (uid, json.dumps({session.WEB_SESSION_SECRET_KEY: stored})),
        )
    forged = session.make_session_token(uid, str(stored).strip())
    parsed = session.parse_session_token(forged)
    assert parsed is not None
    sessions_repo.record_session(uid, parsed[2], "curl", "127.0.0.1")
    # Precondition: the nonce really is active, so resolve can only fail
    # on the secret handling — the test cannot pass for the wrong reason.
    assert sessions_repo.is_session_active(parsed[2]) is True
    assert session.resolve_session_user_id(forged) is None


def test_non_string_secret_rejected_at_every_str_length():
    """Property, not a sample: every ladder non-string is absent.

    The two sides are different in kind. The ACCEPT side IS closed: the
    positive assertion above returns a valid short string unchanged,
    which kills every length floor at once — a floor alone would eat
    short strings. The REJECT side is bounded, not closed: the ladder
    sweeps str() lengths from 1 char up to 300 000 characters, so the
    surviving mutant shape `isinstance(value, str) or len(str(value)) > N`
    fails for every N below that reach and survives for any N at or
    above it — a floor set past 300 000 characters coerces nothing this
    test ever feeds it. That bound is far beyond any plausible real
    implementation, which is what makes a bound enough here.
    Deterministic and dependency-free on purpose: the inputs are
    derived from a fixed size ladder, no generator package. The reach
    assertion below is load-bearing — precisely because a floor above
    the bound survives, if someone shrinks the ladder the test must
    fail on its own bound instead of silently lowering it."""
    key = session.WEB_SESSION_SECRET_KEY
    # str([0] * n) is 3*n chars, so the ladder spans str() lengths
    # 3 .. 300_000; the ints cover the 1..2 char end.
    values = [0, 7, 42] + [[0] * n for n in
                           (1, 2, 5, 10, 34, 100, 334, 1000, 3334, 10000,
                            33334, 100000)]
    assert len(str(values[-1])) > 100_000  # the sweep's documented reach
    for value in values:
        assert not isinstance(value, str)
        # pylint: disable=protected-access
        assert session._stored_session_secret({key: value}) == ""


def test_whitespace_only_session_secret_is_treated_as_absent(auth_db):
    # signed with the RAW stored value, which .strip() must reduce to absent
    #
    # The parametrized non-string test above cannot cover this shape: its
    # body signs with str(stored).strip(), which for a whitespace-only
    # string would sign with "" instead of the stored bytes. Dropping
    # .strip() from _stored_session_secret would adopt " \t\n " as the
    # signing secret and GRANT the forged token — same failure class as
    # the str(None) coercion.
    uid = 4322
    stored = " \t\n "
    with db.auth_conn() as c:
        c.execute(
            "INSERT INTO users (user_id, config) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (user_id) DO UPDATE SET config = EXCLUDED.config",
            (uid, json.dumps({session.WEB_SESSION_SECRET_KEY: stored})),
        )
    forged = session.make_session_token(uid, stored)
    parsed = session.parse_session_token(forged)
    assert parsed is not None
    sessions_repo.record_session(uid, parsed[2], "curl", "127.0.0.1")
    # Precondition: the nonce really is active, so resolve can only fail
    # on the secret handling — the test cannot pass for the wrong reason.
    assert sessions_repo.is_session_active(parsed[2]) is True
    assert session.resolve_session_user_id(forged) is None


def test_guest_session_resolves_without_a_web_session_row(auth_db, monkeypatch):
    """A guest token resolves through the real resolve_session_user_id path.

    Guests are unrecorded BY DESIGN — resolve_session_user_id must bypass
    the nonce lookup for them, or every guest would be logged out."""
    cookie = session.make_guest_session_token()
    parsed = session.parse_session_token(cookie)
    assert parsed is not None
    assert parsed[0] == session.GUEST_USER_ID
    assert sessions_repo.is_session_active(parsed[2]) is False
    assert session.resolve_session_user_id(cookie) == session.GUEST_USER_ID

    # The guest branch returns before ANY auth-DB access, so guests must
    # survive a total auth-DB outage.
    def _down():
        raise RuntimeError("auth DB down")

    monkeypatch.setattr(db, "auth_conn", _down)
    assert session.resolve_session_user_id(cookie) == session.GUEST_USER_ID

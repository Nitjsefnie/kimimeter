"""web_sessions repository — record, lookup, revoke — plus the end-to-end
revocation path through the real app.

Uses the shared `auth_db` fixture from tests/conftest.py. The imports are
top-level: pytest loads conftest.py — and therefore the test DSN
setdefaults — before this module body runs. Nonces use the km- prefix so
this suite can never collide with a sibling service's suite if both are
ever pointed at one scratch database.
"""
from backend import db, sessions_repo
from backend import session as session_mod


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

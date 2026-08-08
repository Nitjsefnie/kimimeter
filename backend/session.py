"""Session token mint/verify + FastAPI auth middleware.

Looks up users in an external auth DB by `user_id`, fetching only the
`config` JSONB column (which carries the PBKDF2 web-password hash and
a per-user web-session secret).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from psycopg import Connection

from backend import db
from backend import sessions_repo


SESSION_COOKIE_NAME = "session"
SESSION_COOKIE_MAX_AGE = 7 * 24 * 3600
WEB_SESSION_SECRET_KEY = "web_session_secret"

# Sentinel user_id reserved for unauthenticated guest sessions.
# Tokens with this user_id are signed with the GUEST_SESSION_SECRET env
# value when it is set, or with a process-local fallback secret when it
# is unset — in which case guest cookies invalidate on restart.
GUEST_USER_ID = 0
_GUEST_SECRET_FALLBACK = secrets.token_urlsafe(32)


def _guest_secret() -> str:
    """Guest-token signing secret.

    Set GUEST_SESSION_SECRET to keep guest sessions valid across
    restarts. Without it a per-process fallback is used and every
    restart signs out every guest.

    The os.environ read is deliberately per-call, not hoisted into a
    module constant: caching it at import time would re-couple signing
    to process start (and break tests that set the variable after
    import).
    """
    return os.environ.get("GUEST_SESSION_SECRET", "").strip() or _GUEST_SECRET_FALLBACK


def parse_session_token(token: str):
    parts = token.split(".")
    if len(parts) != 4:
        return None
    raw_uid, raw_ts, nonce, sig = parts
    try:
        user_id = int(raw_uid)
        issued_at = int(raw_ts)
    except ValueError:
        return None
    if not nonce or not sig:
        return None
    return user_id, issued_at, nonce, sig


def make_session_token(user_id: int, secret: str) -> str:
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(10)
    payload = f"{user_id}.{issued_at}.{nonce}"
    sig = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str, secret: str):
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id, issued_at, _nonce, sig = parsed
    now = int(time.time())
    if issued_at > now + 60:
        return None
    payload = f"{user_id}.{issued_at}.{parsed[2]}"
    expected = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return user_id


def _stored_session_secret(config: dict) -> str:
    """The configured web-session secret, or "" when absent.

    A stored JSON null (or any non-string) is ABSENT, not a value: coercing
    it with str() would turn null into the literal string "None", which
    would then work as a signing secret. Fail closed instead.
    """
    value = config.get(WEB_SESSION_SECRET_KEY)
    return value.strip() if isinstance(value, str) else ""


def get_or_create_session_secret(config: dict) -> str:
    secret = _stored_session_secret(config)
    if secret:
        return secret
    secret = secrets.token_urlsafe(32)
    config[WEB_SESSION_SECRET_KEY] = secret
    return secret


def _query_user_config(c: Connection, user_id: int) -> dict | None:
    row = c.execute(
        "SELECT config FROM users WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    return row[0] if row else None


def load_user_config(user_id: int, conn: Connection | None = None) -> dict | None:
    """Fetch the auth DB's users table.config for one user. Returns None if no row.

    Pass an open conn to share one pool checkout with other lookups on
    the same request (the auth pool is small); omit it to take your own.
    """
    if conn is not None:
        return _query_user_config(conn, user_id)
    with db.auth_conn() as c:
        return _query_user_config(c, user_id)


def write_user_config(user_id: int, config: dict) -> None:
    """Persist a modified config back to the auth DB's users table.

    Used to store the freshly-minted web_session_secret on first login.
    """
    with db.auth_conn() as c:
        c.execute(
            "UPDATE users SET config = %s::jsonb WHERE user_id = %s",
            (json.dumps(config), user_id),
        )


def make_guest_session_token() -> str:
    return make_session_token(GUEST_USER_ID, _guest_secret())


def resolve_session_user_id(token: str) -> int | None:
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id = parsed[0]
    if user_id == GUEST_USER_ID:
        return verify_session_token(token, _guest_secret())
    # Config and nonce state resolve on ONE shared auth-DB connection —
    # the auth pool is max_size=4, and per-request checkouts must not
    # grow beyond the pre-change single load_user_config acquisition
    # plus one (the touch below takes that one). The guest branch above
    # never reaches this lookup: guests have no web_sessions rows, so a
    # lookup would reject every guest.
    with db.auth_conn() as conn:
        config = load_user_config(user_id, conn=conn)
        secret = _stored_session_secret(config) if config else ""
        if not secret:
            return None
        if verify_session_token(token, secret) is None:
            return None
        if not sessions_repo.is_session_active(parsed[2], conn=conn):
            return None
    sessions_repo.touch_session(parsed[2])
    return user_id


def check_origin(request: Request) -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    host = request.headers.get("host", "")
    if not host:
        return True
    if origin:
        return urlparse(origin).netloc == host
    if referer:
        return urlparse(referer).netloc == host
    return False


_AUTH_PUBLIC_PATHS = {"/health", "/login", "/logout", "/login/guest"}


def _admin_deny(request: Request) -> Response | None:
    """Deny-response for an /admin/* request, or None when it may proceed."""
    token = request.headers.get("x-admin-token", "")
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        return JSONResponse(
            {"ok": False, "error": "Unauthorized"}, status_code=401
        )
    if not check_origin(request):
        return Response("Forbidden (cross-origin)", status_code=403)
    return None


def _guest_deny(request: Request, path: str) -> Response | None:
    """Deny-response for a guest request, or None when it may proceed.

    Gates per-session and per-project endpoints from guests, plus
    disallows `project=` filters on aggregate endpoints so guests can
    only see project-mixed data.
    """
    # /api/projects leaks project names (filesystem paths).
    # /api/sessions* covers list, single detail, raw transcript, sidecar.
    # Context-growth-by-session is allowed — just numbers, needed for graphs.
    if path == "/api/projects" or path.startswith("/api/sessions"):
        return JSONResponse(
            {"ok": False, "error": "Forbidden (guest)"}, status_code=403
        )
    # Block project= filtering on aggregate endpoints — guest sees
    # project-mixed data only.
    if request.query_params.get("project"):
        return JSONResponse(
            {"ok": False, "error": "Forbidden (guest cannot filter by project)"},
            status_code=403,
        )
    return None


def _session_cookie_domain() -> str | None:
    """The registrable domain the session cookie is shared across, or None.

    Single source for both cookie helpers — a browser keys a cookie on
    (name, domain, path), so setter and clearer must agree on the domain
    by construction, not by two copies of the same lookup. Read per call,
    not hoisted into a module constant, so tests can set the variable
    after import. Unset → host-only cookie, the pre-sharing behaviour: a
    developer checkout is not served from the shared domain and must not
    try to set a cookie for it.
    """
    return os.environ.get("SESSION_COOKIE_DOMAIN") or None


def set_session_cookie(response, token: str, *, guest: bool = False) -> None:
    """The one place a session cookie is issued.

    Every issuing path must go through this so a new attribute cannot
    reach some responses and not others. Guest cookies are excluded from
    sharing (guest=True): the guest secret is per-service, so a shared
    guest cookie would be rejected by every service except the one that
    issued it and would shadow the host-only one.
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        samesite="strict",
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
        domain=None if guest else _session_cookie_domain(),
    )


def clear_session_cookie(response) -> None:
    """The one place the session cookie is cleared.

    A browser keys a cookie on (name, domain, path), so the deletion must
    agree with set_session_cookie on all three or it writes an expired
    cookie that does not match the live one and logout silently stops
    working. The domain comes from the same _session_cookie_domain()
    source as the setter.

    Both keyings are cleared. The host-only deletion is UNCONDITIONAL:
    with the variable unset it is the one deletion issued, and with it
    set it is the leg that reaches a cookie issued BEFORE the rollout —
    that cookie is keyed (name, host-only, path), so a logout whose
    deletion covers the domain keying alone leaves it in the browser.
    And a surviving pre-rollout host-only cookie CAN still authenticate.
    Two witnesses, each pinned by a test: a guest cookie — its
    resolution consults no web_sessions row at all (guests have none),
    so there is nothing a logout could revoke
    (tests/test_sessions_repo.py::test_guest_cookie_surviving_a_boundary_logout_still_authenticates)
    — and an authenticated cookie naming a session this logout did not
    revoke, as at the two-nonce rollout boundary, where logout revokes
    the presented session and leaves the other live one in place
    (tests/test_sessions_repo.py::test_two_nonce_rollout_boundary_logout_leaves_the_other_session_live).
    The host-only deletion therefore cannot be made conditional on
    anything the logout handler can observe. This rationale is
    deliberately a sufficiency claim with witnesses; it does not
    attempt a characterisation of the cases where a surviving cookie
    stops authenticating.
    """
    domain = _session_cookie_domain()
    if domain is not None:
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            domain=domain,
        )
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
    )


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _AUTH_PUBLIC_PATHS:
        return await call_next(request)
    if path.startswith("/admin/"):
        deny = _admin_deny(request)
        return deny if deny is not None else await call_next(request)
    if not check_origin(request):
        return Response("Forbidden (cross-origin)", status_code=403)
    cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
    user_id = resolve_session_user_id(cookie) if cookie else None
    if user_id is None:
        unauth: Response = (
            JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
            if path.startswith("/api/")
            else RedirectResponse("/login", status_code=302)
        )
        return unauth
    request.state.user_id = user_id
    request.state.is_guest = user_id == GUEST_USER_ID
    if request.state.is_guest:
        deny = _guest_deny(request, path)
        if deny is not None:
            return deny
    return await call_next(request)

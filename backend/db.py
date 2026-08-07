"""Postgres connection pools.

Two pools:
- viz_pool   → kimimeter (this app's tables)
- auth_pool  → the auth DB: reads users.config for auth and UPDATEs it on
               every successful login (write_user_config stores the
               web_session_secret), and reads and writes web_sessions for
               session tracking (login INSERT, throttled last_seen UPDATE,
               revoke UPDATE)

The pools never join across DBs.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import LiteralString, cast
from psycopg_pool import ConnectionPool

_VIZ: ConnectionPool | None = None
_AUTH: ConnectionPool | None = None


def viz_pool() -> ConnectionPool:
    global _VIZ
    if _VIZ is None:
        _VIZ = ConnectionPool(
            os.environ["DATABASE_URL_VIZ"],
            # The read endpoints are sync (blocking psycopg) and run on
            # FastAPI's threadpool, so requests now hit the DB genuinely
            # concurrently — a single dashboard load fans out to several.
            # While they were async-on-the-event-loop they serialised and 8
            # was never exercised; it would now be the bottleneck.
            min_size=2, max_size=20, timeout=10,
            kwargs={"autocommit": False},
        )
    return _VIZ


def auth_pool() -> ConnectionPool:
    global _AUTH
    if _AUTH is None:
        _AUTH = ConnectionPool(
            os.environ["DATABASE_URL_AUTH"],
            min_size=1, max_size=4, timeout=10,
            kwargs={"autocommit": True},
        )
    return _AUTH


def reset_auth_pool() -> None:
    """Close and drop the cached auth pool so the next auth_pool() call
    re-reads DATABASE_URL_AUTH. Test suites need this between scratch-DB
    configurations; production code never calls it."""
    global _AUTH
    if _AUTH is not None:
        try:
            _AUTH.close()
        except Exception:  # noqa: BLE001 — teardown must not fail the test
            pass
    _AUTH = None


def close_viz_pool() -> None:
    """Close and drop the cached viz pool.

    Test teardown uses this after repointing DATABASE_URL_VIZ at a scratch
    database: the next viz_pool() call rebuilds against the new DSN
    instead of serving pooled connections to the dropped one.
    """
    global _VIZ
    if _VIZ is not None:
        try:
            _VIZ.close()
        except Exception:  # noqa: BLE001 — teardown must not fail the test
            pass
    _VIZ = None


def sql_literal(text: str) -> LiteralString:
    """Mark a dynamically assembled query as a literal for the type checker.

    psycopg types execute()'s query parameter as LiteralString so user
    input is pushed through bind parameters. The queries in this codebase
    interpolate only trusted internal fragments — integer bucket widths,
    and filter snippets from api.py's _proj_* helpers, which interpolate
    nothing user-controlled; every user value goes through %s. This
    wrapper says so once, at the source, instead of casting per call site.
    """
    return cast(LiteralString, text)


@contextmanager
def viz_conn():
    with viz_pool().connection() as conn:
        yield conn


@contextmanager
def auth_conn():
    with auth_pool().connection() as conn:
        yield conn


def schema_check() -> None:
    """Fail fast at startup if either DB's required shape is missing.

    For kimimeter: 'files' table exists.
    For the auth DB: 'users' table has a JSONB 'config' column.
    Raises RuntimeError on any mismatch.
    """
    with viz_conn() as c:
        row = c.execute(
            "SELECT to_regclass('public.files')"
        ).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError(
                "kimimeter.files missing — run backend/schema.sql"
            )
    with auth_conn() as c:
        row = c.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' "
            "AND column_name='config'"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "auth DB users table has no 'config' column"
            )
        if row[0] != "jsonb":
            raise RuntimeError(
                f"auth DB users.config must be JSONB, got {row[0]!r}"
            )


def load_dotenv(path: str = ".env") -> None:
    """Tiny dotenv loader. Avoids the python-dotenv dependency.

    Constraints (intentional simplicity — keep values plain):
    - Values are read literally; quotes are NOT stripped. ADMIN_TOKEN="abc"
      stores the value with the literal quotes.
    - The 'export ' prefix is NOT supported (the line key would become
      'export ADMIN_TOKEN', not 'ADMIN_TOKEN').
    - The first '=' splits key from value, so values may contain '=' freely.
    - Existing env vars are NEVER overwritten (uses os.environ.setdefault).
    - Comment lines (starting with '#') and blank lines are skipped.
    """
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

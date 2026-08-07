-- Session records for the web UI, applied to the AUTH database
-- (DATABASE_URL_AUTH), alongside the shared users table. The table is
-- shared across the services that honour the session cookie; this copy of
-- the DDL exists so the test suite can build a scratch auth DB with the
-- same shape. Apply changes through the auth side's canonical schema, not
-- by editing the live table from here.

CREATE TABLE IF NOT EXISTS web_sessions (
  nonce         TEXT PRIMARY KEY,
  user_id       BIGINT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_agent    TEXT NOT NULL DEFAULT '',
  ip            TEXT NOT NULL DEFAULT '',
  revoked_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS web_sessions_user_id_idx
  ON web_sessions (user_id) WHERE revoked_at IS NULL;

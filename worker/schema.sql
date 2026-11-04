CREATE TABLE IF NOT EXISTS subscribers (
  email         TEXT PRIMARY KEY,
  token         TEXT NOT NULL UNIQUE,
  status        TEXT NOT NULL DEFAULT 'pending',   -- pending | active | unsubscribed
  created_at    TEXT NOT NULL,
  confirm_sent  INTEGER NOT NULL DEFAULT 0,
  confirmed_at  TEXT
);

-- signups per IP, for simple abuse limiting
CREATE TABLE IF NOT EXISTS signups (
  ip   TEXT NOT NULL,
  at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signups_ip_at ON signups (ip, at);

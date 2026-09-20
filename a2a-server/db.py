import sqlite3

# Schema is created fresh; no migration machinery — there are no deployed DBs yet.
# When the schema changes before first release, delete the db file and restart.
SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  domain_id      TEXT NOT NULL,
  agent_id       TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  description    TEXT NOT NULL DEFAULT '',
  accept_policy  TEXT NOT NULL DEFAULT 'notify_run',
  accept_from    TEXT NOT NULL DEFAULT 'all',
  default_driver TEXT NOT NULL DEFAULT 'command',
  default_driver_kind TEXT NOT NULL DEFAULT 'command',
  session        TEXT NOT NULL DEFAULT '',
  last_seen_at   REAL,
  registered_at  REAL NOT NULL,
  PRIMARY KEY (domain_id, agent_id)
);
CREATE TABLE IF NOT EXISTS owner_bindings (
  domain_id    TEXT NOT NULL,
  username     TEXT NOT NULL,
  channel      TEXT NOT NULL,
  id_type      TEXT NOT NULL,
  id           TEXT NOT NULL,
  platform_uid TEXT NOT NULL DEFAULT '',
  verified     INTEGER NOT NULL DEFAULT 0,
  updated_at   REAL NOT NULL,
  PRIMARY KEY (domain_id, username, channel)
);
CREATE TABLE IF NOT EXISTS tasks (
  id               TEXT PRIMARY KEY,
  domain_id        TEXT NOT NULL,
  initiator        TEXT NOT NULL,
  target           TEXT NOT NULL,
  text             TEXT NOT NULL,
  convo            TEXT NOT NULL DEFAULT '[]',
  status           TEXT NOT NULL,
  fail_reason      TEXT,
  lease_id         TEXT,
  lease_session    TEXT,
  timeout_seconds  INTEGER NOT NULL DEFAULT 3600,
  expected_seconds INTEGER,
  created_at       REAL NOT NULL,
  available_at     REAL,
  dispatched_at    REAL,
  started_at       REAL,
  ir_at            REAL,
  finished_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_target ON tasks(domain_id, target, status);
CREATE INDEX IF NOT EXISTS idx_tasks_init   ON tasks(domain_id, initiator, status);
CREATE TABLE IF NOT EXISTS task_events (
  domain_id  TEXT NOT NULL,
  task_id    TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  PRIMARY KEY (domain_id, task_id, seq)
);
CREATE TABLE IF NOT EXISTS approvals (
  task_id    TEXT PRIMARY KEY,
  domain_id  TEXT NOT NULL,
  state      TEXT NOT NULL,
  expires_at REAL NOT NULL,
  decided_at REAL
);
"""

INFLIGHT = ("dispatched", "working", "input-required")


def connect(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

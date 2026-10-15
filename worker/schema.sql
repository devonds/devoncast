CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- 'script' or 'url'
  payload TEXT NOT NULL,           -- JSON tool arguments
  title TEXT,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued, claimed, accepted, rendering, retrying, published, failed
  episode_id TEXT,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs (status, created_at);

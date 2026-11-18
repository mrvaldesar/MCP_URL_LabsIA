ALTER TABLE submissions ADD COLUMN verification_retry_next_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE submissions ADD COLUMN verification_retry_started_at INTEGER NOT NULL DEFAULT 0;

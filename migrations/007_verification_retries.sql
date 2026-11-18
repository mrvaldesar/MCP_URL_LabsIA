ALTER TABLE submissions ADD COLUMN verification_retry_count INTEGER NOT NULL DEFAULT 0
    CHECK (verification_retry_count >= 0);
ALTER TABLE submissions ADD COLUMN verification_retry_lease_until INTEGER NOT NULL DEFAULT 0;

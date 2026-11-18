ALTER TABLE teams ADD COLUMN repository_verification_status TEXT NOT NULL DEFAULT 'verificado'
    CHECK (repository_verification_status IN ('verificado', 'pendiente', 'inconsistente'));
ALTER TABLE teams ADD COLUMN repository_verification_next_at INTEGER NOT NULL DEFAULT 0;
UPDATE teams SET repository_verification_status = 'pendiente' WHERE github_repository IS NULL;

ALTER TABLE submissions ADD COLUMN github_repository TEXT;
ALTER TABLE submissions ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'sin_verificar'
    CHECK (verification_status IN ('sin_verificar', 'verificado', 'pendiente', 'inconsistente'));
ALTER TABLE submissions ADD COLUMN verified_ci_status TEXT NOT NULL DEFAULT 'desconocido';
CREATE INDEX submissions_pending_verification ON submissions(id) WHERE verification_status = 'pendiente';

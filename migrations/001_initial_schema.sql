CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT COLLATE NOCASE UNIQUE,
    token_hash TEXT NOT NULL,
    salt TEXT NOT NULL UNIQUE,
    section TEXT NOT NULL,
    modality TEXT NOT NULL,
    members TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_identities (
    github_user TEXT COLLATE NOCASE UNIQUE,
    carnet TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL REFERENCES teams (id),
    deliverable_id TEXT NOT NULL,
    label TEXT,
    repo_url TEXT,
    commit_hash TEXT NOT NULL,
    declared_metrics_json TEXT NOT NULL,
    declared_is_ci_passing BOOLEAN NOT NULL,
    received_at_utc TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    UNIQUE (team_id, deliverable_id, attempt)
);

ALTER TABLE teams ADD COLUMN github_repository TEXT COLLATE NOCASE;

CREATE UNIQUE INDEX teams_github_repository_unique ON teams (github_repository);

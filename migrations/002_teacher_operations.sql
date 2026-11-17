CREATE TABLE roster (
    carnet TEXT PRIMARY KEY NOT NULL,
    github_user TEXT COLLATE NOCASE UNIQUE NOT NULL,
    section TEXT NOT NULL CHECK (section IN ('01', '02'))
);

CREATE TABLE registration_requests (
    ip TEXT NOT NULL,
    requested_at REAL NOT NULL
);

CREATE INDEX registration_requests_time ON registration_requests (requested_at);
CREATE INDEX registration_requests_ip ON registration_requests (ip, requested_at);

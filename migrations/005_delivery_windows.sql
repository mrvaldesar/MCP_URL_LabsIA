CREATE TABLE delivery_windows (
    section TEXT NOT NULL CHECK (section IN ('01', '02')),
    deliverable_id TEXT NOT NULL CHECK (deliverable_id IN ('D1','D2','D3','D4','D5','D6','D7','D8','D9','D10')),
    opens_at_utc TEXT NOT NULL,
    closes_at_utc TEXT NOT NULL,
    CHECK (opens_at_utc < closes_at_utc),
    PRIMARY KEY (section, deliverable_id)
);

ALTER TABLE submissions ADD COLUMN received_section TEXT CHECK (received_section IN ('01', '02'));
UPDATE submissions SET received_section = (SELECT section FROM teams WHERE teams.id = submissions.team_id);
ALTER TABLE submissions ADD COLUMN timing TEXT NOT NULL DEFAULT 'sin_plazo'
    CHECK (timing IN ('a_tiempo', 'tardío', 'sin_plazo'));
ALTER TABLE submissions ADD COLUMN late_minutes INTEGER NOT NULL DEFAULT 0 CHECK (late_minutes >= 0);

ALTER TABLE submissions ADD COLUMN received_team_name TEXT;
ALTER TABLE submissions ADD COLUMN received_modality TEXT;
ALTER TABLE submissions ADD COLUMN received_members TEXT;

UPDATE submissions SET
    received_team_name = (SELECT team_name FROM teams WHERE id = submissions.team_id),
    received_modality = (SELECT modality FROM teams WHERE id = submissions.team_id),
    received_members = (SELECT members FROM teams WHERE id = submissions.team_id);

CREATE INDEX submissions_received_section ON submissions(received_section, id);

import csv
from contextlib import closing
from pathlib import Path

import sqlite3

from src import database


def _spreadsheet_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_section(section: str, output, db_path=None):
    path = Path(db_path if db_path is not None else database.DB_PATH).resolve()
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        rows = conn.execute("""
            SELECT s.id AS receipt_id, s.team_id, s.received_team_name AS team_name, s.received_section AS section,
                   s.received_modality AS modality, s.received_members AS members, s.deliverable_id, s.label, s.commit_hash,
                   s.declared_metrics_json, s.declared_is_ci_passing, s.received_at_utc, s.attempt,
                   s.verification_status, s.verified_ci_status, s.timing, s.late_minutes
            FROM submissions AS s
            WHERE s.received_section = ? ORDER BY s.id
        """, (section,))
        writer = csv.writer(output)
        writer.writerow(column[0] for column in rows.description)
        writer.writerows(tuple(_spreadsheet_cell(value) for value in row) for row in rows)

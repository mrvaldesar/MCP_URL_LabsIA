import sqlite3
import os
import json
import secrets
from datetime import datetime

DB_PATH = "data/grader.db"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10) as conn:
        cursor = conn.cursor()

        # Create teams table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_token TEXT UNIQUE NOT NULL,
                section TEXT NOT NULL,
                modality TEXT NOT NULL,
                members TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # Create submissions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                deliverable_id TEXT NOT NULL,
                repo_url TEXT NOT NULL,
                commit_hash TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                is_ci_passing BOOLEAN NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (team_id) REFERENCES teams (id)
            )
        """)

        conn.commit()

def register_team_db(section: str, modality: str, members: list[dict]) -> str:
    team_token = secrets.token_hex(16)
    members_json = json.dumps(members)
    created_at = datetime.utcnow().isoformat()

    with sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO teams (team_token, section, modality, members, created_at) VALUES (?, ?, ?, ?, ?)",
            (team_token, section, modality, members_json, created_at)
        )
        conn.commit()

    return team_token

def save_submission_db(team_token: str, deliverable_id: str, repo_url: str, commit_hash: str, metrics_json: str, is_ci_passing: bool) -> int:
    with sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM teams WHERE team_token = ?", (team_token,))
        result = cursor.fetchone()

        if not result:
            raise ValueError(f"El team_token '{team_token}' no existe o es inválido.")

        team_id = result[0]
        timestamp = datetime.utcnow().isoformat()

        cursor.execute(
            "INSERT INTO submissions (team_id, deliverable_id, repo_url, commit_hash, metrics_json, is_ci_passing, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (team_id, deliverable_id, repo_url, commit_hash, metrics_json, is_ci_passing, timestamp)
        )
        conn.commit()
        return cursor.lastrowid

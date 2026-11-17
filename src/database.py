import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

DB_PATH = "data/grader.db"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class Receipt(TypedDict):
    receipt_id: int
    deliverable_id: str
    label: str
    received_at_utc: str
    status: str
    attempt: int
    section: str


@contextmanager
def _connect():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            yield conn


def _token_hash(token: str, salt: str) -> str:
    return hmac.digest(bytes.fromhex(salt), token.encode("utf-8"), hashlib.sha256).hex()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("BEGIN IMMEDIATE")
        legacy = any(row[1] == "team_token" for row in conn.execute("PRAGMA table_info(teams)"))
        if legacy:
            conn.execute("ALTER TABLE submissions RENAME TO legacy_submissions")
            conn.execute("ALTER TABLE teams RENAME TO legacy_teams")

        _apply_migrations(conn)
        if legacy:
            _migrate_legacy_rows(conn)
    # Remove WAL frames that could still contain legacy tokens.
    with _connect() as conn:
        busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise RuntimeError("No se pudo truncar el WAL de SQLite. Cierra las otras conexiones y vuelve a iniciar.")


def _apply_migrations(conn: sqlite3.Connection):
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    migrations = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    if not migrations:
        raise RuntimeError("No se encontraron las migraciones de SQLite.")
    for path in migrations:
        if conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (path.name,)).fetchone():
            continue
        statement = ""
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ""
        if statement.strip():
            raise RuntimeError(f"Migración SQL incompleta: {path.name}")
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (path.name,))


def _migrate_legacy_rows(conn: sqlite3.Connection):
    for team_id, token, section, modality, members, created_at in conn.execute("""
        SELECT id, team_token, section, modality, members, created_at FROM legacy_teams
    """):
        salt = secrets.token_hex(16)
        conn.execute("""
            INSERT INTO teams (id, token_hash, salt, section, modality, members, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (team_id, _token_hash(token, salt), salt, section, modality, members, created_at))
        conn.executemany("""
            INSERT INTO member_identities (carnet) VALUES (?)
            ON CONFLICT(carnet) DO NOTHING
        """, ((str(member["carnet"]),) for member in json.loads(members)))
    conn.execute("""
        INSERT INTO submissions (
            id, team_id, deliverable_id, repo_url, commit_hash, declared_metrics_json,
            declared_is_ci_passing, received_at_utc, attempt
        )
        SELECT id, team_id, deliverable_id, repo_url, commit_hash, metrics_json,
            is_ci_passing,
            CASE WHEN timestamp LIKE '%+00:00' THEN timestamp ELSE timestamp || '+00:00' END,
            ROW_NUMBER() OVER (PARTITION BY team_id, deliverable_id ORDER BY id)
        FROM legacy_submissions
    """)
    conn.execute("DROP TABLE legacy_submissions")
    conn.execute("DROP TABLE legacy_teams")


def _new_token() -> tuple[str, str, str]:
    salt = secrets.token_hex(16)
    token = f"{salt}.{secrets.token_hex(32)}"
    return token, salt, _token_hash(token, salt)


def register_team_db(team_name: str, section: str, modality: str, members: list[dict]) -> str:
    token, salt, token_hash = _new_token()
    with _connect() as conn:
        try:
            conn.execute("""
                INSERT INTO teams (team_name, token_hash, salt, section, modality, members, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                team_name, token_hash, salt, section, modality,
                json.dumps(members), datetime.now(timezone.utc).isoformat(),
            ))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: teams.team_name" in str(exc):
                raise ValueError("El nombre del equipo ya está registrado.") from None
            raise
        try:
            conn.executemany("""
                INSERT INTO member_identities (github_user, carnet) VALUES (?, ?)
            """, ((member["github_user"], member["carnet"]) for member in members))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: member_identities." in str(exc):
                raise ValueError("Hay un usuario GitHub o carné repetido o ya registrado.") from None
            raise
    return token


def resolve_team_id(token: str) -> int | None:
    with _connect() as conn:
        salt, separator, _ = token.partition(".")
        if separator:
            candidates = conn.execute("""
                SELECT id, token_hash, salt FROM teams WHERE salt = ?
            """, (salt,))
        else:
            # Previous releases issued tokens without a public lookup prefix.
            candidates = conn.execute("""
                SELECT id, token_hash, salt FROM teams WHERE team_name IS NULL
            """)
        for team_id, token_hash, salt in candidates:
            if hmac.compare_digest(token_hash, _token_hash(token, salt)):
                return team_id
        return None


def save_submission_db(
    team_id: int,
    deliverable_id: str,
    label: str,
    commit_hash: str,
    metrics_json: str,
    is_ci_passing: bool,
) -> Receipt:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        team = conn.execute("SELECT section FROM teams WHERE id = ?", (team_id,)).fetchone()
        if team is None:
            raise ValueError("El equipo autenticado ya no existe.")
        section = team[0]
        attempt = conn.execute("""
            SELECT COALESCE(MAX(attempt), 0) + 1 FROM submissions
            WHERE team_id = ? AND deliverable_id = ?
        """, (team_id, deliverable_id)).fetchone()[0]
        received_at = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute("""
            INSERT INTO submissions (
                team_id, deliverable_id, label, commit_hash, declared_metrics_json,
                declared_is_ci_passing, received_at_utc, attempt
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (team_id, deliverable_id, label, commit_hash, metrics_json, is_ci_passing, received_at, attempt))
        return Receipt(
            receipt_id=cursor.lastrowid,
            deliverable_id=deliverable_id,
            label=label,
            received_at_utc=received_at,
            status="received",
            attempt=attempt,
            section=section,
        )


def rotate_team_token(team_id: int) -> str | None:
    token, salt, token_hash = _new_token()
    with _connect() as conn:
        updated = conn.execute("""
            UPDATE teams SET token_hash = ?, salt = ? WHERE id = ?
        """, (token_hash, salt, team_id))
        return token if updated.rowcount == 1 else None


def replace_roster(section: str, members: list[tuple[str, str]]):
    with _connect() as conn:
        conn.execute("DELETE FROM roster WHERE section = ?", (section,))
        conn.executemany("""
            INSERT INTO roster (section, github_user, carnet) VALUES (?, ?, ?)
        """, ((section, github_user, carnet) for github_user, carnet in members))


def check_health():
    with _connect() as conn:
        conn.execute("SELECT name FROM schema_migrations LIMIT 1").fetchone()


def registration_retry_after(ip: str) -> int | None:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        conn.execute("DELETE FROM registration_requests WHERE requested_at <= ?", (now - 60,))
        count, oldest = conn.execute("""
            SELECT COUNT(*), MIN(requested_at) FROM registration_requests WHERE ip = ?
        """, (ip,)).fetchone()
        if count >= 5:
            return max(1, math.ceil(oldest + 60 - now))
        conn.execute("INSERT INTO registration_requests VALUES (?, ?)", (ip, now))
    return None

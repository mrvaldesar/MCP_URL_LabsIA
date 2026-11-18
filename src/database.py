import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import TypedDict

from src.audit import logger
from src.models import TEAM_SIZES, Modality, validate_team_size

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
    verification_status: str
    verified_ci_status: str
    timing: str
    late_minutes: int


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


def _migration_files() -> list[Path]:
    return sorted(path for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql") if path.is_file())


def _apply_migrations(conn: sqlite3.Connection):
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    migrations = _migration_files()
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
            INSERT INTO teams (id, token_hash, salt, section, modality, members, created_at, repository_verification_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pendiente')
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


class MemberIdentityConflict(ValueError):
    pass


def _claim_member_identities(conn: sqlite3.Connection, members: list[dict]):
    try:
        conn.executemany("""
            INSERT INTO member_identities (github_user, carnet) VALUES (?, ?)
        """, ((member.get("github_user"), str(member["carnet"])) for member in members))
    except sqlite3.IntegrityError as exc:
        if "UNIQUE constraint failed: member_identities." in str(exc):
            raise MemberIdentityConflict("Hay un usuario GitHub o carné repetido o ya registrado.") from None
        raise


class RosterMismatch(ValueError):
    def __init__(self, carnets):
        super().__init__("Cada integrante debe figurar en el padrón de la sección con su usuario GitHub y carné. Verifica los datos o consulta al docente.")
        self.carnets = carnets


def strict_roster() -> bool:
    return os.getenv("STRICT_ROSTER", "").strip().lower() not in {"0", "false", "no", "off"}


def roster_is_empty() -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM roster LIMIT 1").fetchone() is None


def _check_roster(conn: sqlite3.Connection, section: str, members: list[dict]) -> int:
    unmatched = [
        member["carnet"] for member in members if conn.execute("""
            SELECT 1 FROM roster WHERE section = ? AND github_user = ? AND carnet = ?
        """, (section, member["github_user"], member["carnet"])).fetchone() is None
    ]
    if unmatched and strict_roster():
        raise RosterMismatch(unmatched)
    return len(unmatched)


def register_team_db(team_name: str, section: str, modality: Modality, members: list[dict], github_repository: str,
                     repository_status: str = "verificado", retry_at: int = 0) -> str:
    token, salt, token_hash = _new_token()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        unmatched = _check_roster(conn, section, members)
        try:
            conn.execute("""
                INSERT INTO teams (team_name, token_hash, salt, section, modality, members, created_at, github_repository,
                    repository_verification_status, repository_verification_next_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                team_name, token_hash, salt, section, modality,
                json.dumps(members), datetime.now(timezone.utc).isoformat(), github_repository, repository_status, retry_at,
            ))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: teams.github_repository" in str(exc):
                raise ValueError("El repositorio ya está asignado a otro equipo. Consulta al docente.") from None
            if "UNIQUE constraint failed: teams.team_name" in str(exc):
                raise ValueError("El nombre del equipo ya está registrado.") from None
            raise
        _claim_member_identities(conn, members)
    if unmatched:
        logger.warning("Registro fuera del padrón sección=%s integrantes_sin_coincidencia=%d", section, unmatched)
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


def get_team_repository(team_id: int) -> str | None:
    with _connect() as conn:
        team = conn.execute("SELECT github_repository FROM teams WHERE id = ?", (team_id,)).fetchone()
        return team[0] if team else None


def save_submission_db(
    team_id: int,
    deliverable_id: str,
    label: str,
    commit_hash: str,
    metrics_json: str,
    is_ci_passing: bool,
    repository: str,
    verification_status: str = "verificado",
    verified_ci_status: str = "desconocido",
    received_at: datetime | None = None,
    retry_at: int = 0,
) -> Receipt:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        team = conn.execute(
            "SELECT section, github_repository, team_name, modality, members FROM teams WHERE id = ?", (team_id,)
        ).fetchone()
        if team is None:
            raise ValueError("El equipo autenticado ya no existe.")
        section, assigned_repository, team_name, modality, members = team
        if assigned_repository != repository:
            raise ValueError("Cambió el repositorio asignado al equipo. Vuelve a intentar la entrega.")
        attempt = conn.execute("""
            SELECT COALESCE(MAX(attempt), 0) + 1 FROM submissions
            WHERE team_id = ? AND deliverable_id = ?
        """, (team_id, deliverable_id)).fetchone()[0]
        received = received_at or datetime.now(timezone.utc)
        timing, late_minutes = delivery_timing(conn, section, deliverable_id, received)
        received_at = received.isoformat()
        cursor = conn.execute("""
            INSERT INTO submissions (
                team_id, deliverable_id, label, commit_hash, declared_metrics_json,
                declared_is_ci_passing, received_at_utc, attempt, github_repository, verification_status,
                verified_ci_status, received_section, timing, late_minutes,
                received_team_name, received_modality, received_members, verification_retry_next_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (team_id, deliverable_id, label, commit_hash, metrics_json, is_ci_passing, received_at, attempt,
              repository, verification_status, verified_ci_status, section, timing, late_minutes,
              team_name, modality, members, retry_at))
        return Receipt(
            receipt_id=cursor.lastrowid,
            deliverable_id=deliverable_id,
            label=label,
            received_at_utc=received_at,
            status="received",
            attempt=attempt,
            section=section,
            verification_status=verification_status,
            verified_ci_status=verified_ci_status,
            timing=timing,
            late_minutes=late_minutes,
        )


def update_team(
    team_id: int, members: list[dict] | None, modality: Modality | None, section: str | None = None
) -> str | None:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        team = conn.execute(
            "SELECT modality, members FROM teams WHERE id = ? AND (? IS NULL OR section = ?)",
            (team_id, section, section),
        ).fetchone()
        if team is None:
            return None

        current_modality, stored_members = team
        current_members = json.loads(stored_members)
        replace_members = members is not None
        if members is None:
            members = current_members
            modality = modality or current_modality
        elif modality is None:
            modality = next((name for name, size in TEAM_SIZES.items() if size == len(members)), None)
            if modality is None:
                raise ValueError("El equipo debe tener entre uno y tres integrantes.")
        validate_team_size(modality, len(members))

        if replace_members:
            for member in current_members:
                # Legacy teams can share a carnet, with a single claim created during migration.
                conn.execute("""
                    DELETE FROM member_identities
                    WHERE carnet = ? AND github_user IS ?
                      AND (github_user IS NOT NULL OR NOT EXISTS (
                          SELECT 1 FROM teams AS other, json_each(other.members) AS member
                          WHERE other.id != ?
                            AND CAST(json_extract(member.value, '$.carnet') AS TEXT) = member_identities.carnet
                      ))
                """, (str(member["carnet"]), member.get("github_user"), team_id))
            _claim_member_identities(conn, members)
        token, salt, token_hash = _new_token()
        conn.execute("""
            UPDATE teams SET modality = ?, members = ?, salt = ?, token_hash = ? WHERE id = ?
        """, (modality, json.dumps(members), salt, token_hash, team_id))
    return token


def rotate_team_token(team_id: int, section: str | None = None) -> str | None:
    token, salt, token_hash = _new_token()
    with _connect() as conn:
        updated = conn.execute("""
            UPDATE teams SET token_hash = ?, salt = ? WHERE id = ? AND (? IS NULL OR section = ?)
        """, (token_hash, salt, team_id, section, section))
        return token if updated.rowcount == 1 else None


def replace_roster(section: str, members: list[tuple[str, str]]):
    with _connect() as conn:
        conn.execute("DELETE FROM roster WHERE section = ?", (section,))
        conn.executemany("""
            INSERT INTO roster (section, github_user, carnet) VALUES (?, ?, ?)
        """, ((section, github_user, carnet) for github_user, carnet in members))


def check_health() -> bool:
    expected = {path.name for path in _migration_files()}
    with _connect() as conn:
        applied = {name for (name,) in conn.execute("SELECT name FROM schema_migrations")}
    return bool(expected) and applied == expected

def pending_verification_ids(max_attempts: int, window: int = 172800) -> list[int]:
    with _connect() as conn:
        return [row[0] for row in conn.execute("""
            SELECT id FROM submissions WHERE verification_status = 'pendiente'
              AND verification_retry_count < ? AND verification_retry_lease_until <= unixepoch()
              AND verification_retry_next_at <= unixepoch()
              AND COALESCE(NULLIF(verification_retry_started_at, 0), unixepoch(received_at_utc)) + ? > unixepoch()
            ORDER BY verification_retry_count, id LIMIT 20
        """, (max_attempts, window))]


def aged_pending_count(minutes: int) -> int:
    with _connect() as conn:
        return conn.execute("""
            SELECT COUNT(*) FROM submissions WHERE verification_status = 'pendiente'
              AND julianday(received_at_utc) < julianday('now', ?)
        """, (f"-{minutes} minutes",)).fetchone()[0]


def claim_verification(receipt_id: int, section: str | None, max_attempts: int, window: int, credential_alert: bool, rate_limit_until: int):
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT github_repository, label, commit_hash, verification_retry_count,
                   verification_retry_lease_until > unixepoch(), verification_retry_next_at,
                   COALESCE(NULLIF(verification_retry_started_at, 0), unixepoch(received_at_utc)), unixepoch()
            FROM submissions WHERE id = ? AND verification_status = 'pendiente'
              AND (? IS NULL OR received_section = ?)
        """, (receipt_id, section, section)).fetchone()
        if row is None:
            return "ausente", None
        repository, label, sha, count, leased, next_at, started, now = row
        if leased:
            return "en_curso", None
        if started is None or started + window <= now or count >= max_attempts:
            return "agotado", None
        next_at = max(next_at, rate_limit_until)
        if credential_alert or next_at > now:
            return "programado", (count, next_at)
        lease = now + 600
        conn.execute("""
            UPDATE submissions SET verification_retry_count = ?, verification_retry_lease_until = ?
            WHERE id = ?
        """, (count + 1, lease, receipt_id))
        return "tomado", (repository, label, sha, count + 1, lease)


def finish_verification(receipt_id: int, lease: int, count: int, sha: str, status: str, ci: str, next_at: int) -> bool:
    with _connect() as conn:
        return conn.execute("""
            UPDATE submissions SET commit_hash = ?, verification_status = ?, verified_ci_status = ?,
                verification_retry_lease_until = 0, verification_retry_count = ?, verification_retry_next_at = ?
            WHERE id = ? AND verification_status = 'pendiente' AND verification_retry_lease_until = ?
        """, (sha, status, ci, count, next_at, receipt_id, lease)).rowcount == 1


def reset_verification(receipt_id: int, section: str | None) -> str:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT verification_retry_lease_until > unixepoch()
            FROM submissions WHERE id = ? AND verification_status = 'pendiente'
              AND (? IS NULL OR received_section = ?)
        """, (receipt_id, section, section)).fetchone()
        if row is None:
            return "ausente"
        if row[0]:
            return "en_curso"
        conn.execute("""
            UPDATE submissions SET verification_retry_count = 0, verification_retry_lease_until = 0,
                verification_retry_started_at = unixepoch()
            WHERE id = ?
        """, (receipt_id,))
        return "reiniciado"


def delivery_timing(conn, section, deliverable_id, received):
    window = conn.execute("""
        SELECT opens_at_utc, closes_at_utc FROM delivery_windows
        WHERE section = ? AND deliverable_id = ?
    """, (section, deliverable_id)).fetchone()
    if window is None:
        return "sin_plazo", 0
    opens, closes = (datetime.fromisoformat(value) for value in window)
    if received < opens:
        return "sin_plazo", 0
    if received > closes:
        return "tardío", ceil((received - closes).total_seconds() / 60)
    return "a_tiempo", 0


def set_delivery_window(section, deliverable_id, opens, closes):
    with _connect() as conn:
        conn.execute("""
            INSERT INTO delivery_windows (section, deliverable_id, opens_at_utc, closes_at_utc)
            VALUES (?, ?, ?, ?) ON CONFLICT (section, deliverable_id) DO UPDATE SET
                opens_at_utc = excluded.opens_at_utc, closes_at_utc = excluded.closes_at_utc
        """, (section, deliverable_id, opens, closes))

def list_teams(section: str | None) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, team_name, section, modality, members, github_repository, created_at, repository_verification_status
            FROM teams WHERE (? IS NULL OR section = ?) ORDER BY id
        """, (section, section))
        return [dict(row) | {"members": json.loads(row["members"])} for row in rows]


def team_assignment(team_id: int, section: str | None):
    with _connect() as conn:
        return conn.execute("""
            SELECT section, github_repository FROM teams WHERE id = ? AND (? IS NULL OR section = ?)
        """, (team_id, section, section)).fetchone()


def change_assignment(team_id: int, previous: tuple, section: str, repository: str | None) -> bool:
    with _connect() as conn:
        try:
            return conn.execute("""
                UPDATE teams SET section = ?, github_repository = ?,
                    repository_verification_status = CASE WHEN ? IS NULL THEN 'pendiente' ELSE 'verificado' END,
                    repository_verification_next_at = 0
                WHERE id = ? AND section = ? AND github_repository IS ?
            """, (section, repository, repository, team_id, *previous)).rowcount == 1
        except sqlite3.IntegrityError as exc:
            if "teams.github_repository" not in str(exc):
                raise
            raise ValueError("El repositorio ya está asignado a otro equipo. Consulta al docente.") from None

def pending_repositories():
    with _connect() as conn:
        return conn.execute("""
            SELECT id, github_repository, section FROM teams
            WHERE repository_verification_status = 'pendiente' AND github_repository IS NOT NULL
              AND repository_verification_next_at <= unixepoch()
            ORDER BY repository_verification_next_at, id LIMIT 20
        """).fetchall()


def update_repository_verification(team_id, repository, section, status, next_at):
    with _connect() as conn:
        return conn.execute("""
            UPDATE teams SET repository_verification_status = ?, repository_verification_next_at = ?
            WHERE id = ? AND github_repository = ? AND section = ? AND repository_verification_status = 'pendiente'
        """, (status, next_at, team_id, repository, section)).rowcount == 1

def verification_health_counts(minutes: int, max_attempts: int, window: int) -> dict[str, int]:
    with _connect() as conn:
        receipts = conn.execute("""
            SELECT COUNT(*),
                COUNT(CASE WHEN julianday(received_at_utc) < julianday('now', ?) THEN 1 END),
                COUNT(CASE WHEN verification_retry_count >= ?
                     OR COALESCE(NULLIF(verification_retry_started_at, 0), unixepoch(received_at_utc)) + ?
                        <= unixepoch() THEN 1 END)
            FROM submissions WHERE verification_status = 'pendiente'
        """, (f"-{minutes} minutes", max_attempts, window)).fetchone()
        repositories = conn.execute("""
            SELECT COUNT(CASE WHEN repository_verification_status = 'pendiente' THEN 1 END),
                   COUNT(CASE WHEN repository_verification_status = 'inconsistente' THEN 1 END)
            FROM teams WHERE github_repository IS NOT NULL
        """).fetchone()
    return dict(zip(("pending_receipts", "stale_pending_receipts", "exhausted_pending_receipts",
                     "pending_repositories", "inconsistent_repositories"), (*receipts, *repositories)))

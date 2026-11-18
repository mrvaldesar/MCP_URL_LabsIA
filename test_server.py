import csv
import io
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from starlette.testclient import TestClient

from src import database
from src.server import create_server


@pytest.fixture
def grader_db(tmp_path, monkeypatch):
    path = tmp_path / "grader.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setenv("GRADER_LOG_PATH", str(tmp_path / "grader.log"))
    monkeypatch.setenv("ADMIN_TOKEN", "admin-test-credential")
    database.init_db()
    return path


def rpc(client, method, params=None, token=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    })


def register(client, team_name, members, modality="individual", section="02"):
    return rpc(client, "tools/call", {
        "name": "register_team",
        "arguments": {
            "team_name": team_name, "section": section, "modality": modality,
            "members": members,
        },
    })


MEMBER = {"github_user": "alumna-uno", "carnet": "1234524"}


@pytest.fixture
def client(grader_db):
    app = create_server().streamable_http_app()
    with TestClient(app, base_url="http://localhost:8000") as client:
        yield client


def test_register_is_public_and_keeps_the_token_out_of_sqlite_and_mcp(client, grader_db):
    response = register(client, "Equipo Uno", [MEMBER])
    assert response.status_code == 200
    assert response.json()["result"].get("isError") is False
    token = response.headers["X-Team-Token"]
    assert response.headers["Cache-Control"] == "no-store"
    assert token not in response.text

    with closing(sqlite3.connect(grader_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM teams").fetchone()
        assert "team_token" not in row.keys()
        assert token not in repr(tuple(row))
        assert token.split(".")[1] not in repr(tuple(row))
        assert len(row["token_hash"]) == 64


def test_duplicate_member_rolls_back_team_and_identity_claims(client):
    assert register(client, "Primero", [MEMBER]).headers.get("X-Team-Token")
    new_member = {"github_user": "otro", "carnet": "1234624"}
    duplicate = {"github_user": "ALUMNA-UNO", "carnet": "1234724"}
    rejected = register(client, "Segundo", [new_member, duplicate], "pareja")
    assert rejected.json()["result"]["isError"]
    assert "X-Team-Token" not in rejected.headers
    assert register(client, "Segundo", [new_member]).headers.get("X-Team-Token")


def test_submission_requires_bearer_and_uses_authenticated_section(client, grader_db):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    arguments = {
        "deliverable_id": "D1", "label": "primera", "commit_hash": "abc1234",
        "metrics_json": "{}", "is_ci_passing": True,
    }
    params = {"name": "submit_lab_results", "arguments": arguments}
    for invalid in (None, token.rsplit(".", 1)[0] + "." + "0" * 64):
        response = rpc(client, "tools/call", params, token=invalid)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "Authorization: Bearer" in response.json()["error"]

    first = rpc(client, "tools/call", params, token=token).json()["result"]["structuredContent"]
    arguments.update(label="segunda", is_ci_passing=False)
    second = rpc(client, "tools/call", params, token=token).json()["result"]["structuredContent"]
    assert first == {
        "receipt_id": 1, "deliverable_id": "D1", "label": "primera", "section": "02",
        "status": "received", "attempt": 1, "received_at_utc": first["received_at_utc"],
    }
    assert datetime.fromisoformat(first["received_at_utc"]).utcoffset() == timedelta(0)
    assert second["attempt"] == 2
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("""
            SELECT declared_metrics_json, declared_is_ci_passing FROM submissions ORDER BY id
        """).fetchall() == [("{}", 1), ("{}", 0)]


def test_discovery_annotations_and_oauth_routes(client):
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"register_team", "submit_lab_results"}
    for tool in tools:
        assert tool["annotations"] == {
            "readOnlyHint": False, "idempotentHint": False, "destructiveHint": False,
        }
        assert not {"ctx", "team_id", "token"} & tool["inputSchema"]["properties"].keys()
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp"):
        assert client.get(path).status_code == 404


def test_health_without_authentication(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload.keys() == {"status", "timestamp"}
    assert payload["status"] == "ok"
    assert datetime.fromisoformat(payload["timestamp"]).utcoffset() == timedelta(0)


def test_teacher_routes_and_audit(client, grader_db):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    student = {"Authorization": f"Bearer {token}"}
    admin_token = "admin-test-credential"
    admin = {"Authorization": f"Bearer {admin_token}"}
    assert client.get("/admin/export?section=02", headers=student).status_code == 403
    assert client.post("/admin/teams/1/rotate-token", headers=student).status_code == 403
    assert client.post("/admin/roster", headers=student).status_code == 403

    roster = {"section": "02", "members": [MEMBER]}
    assert client.post("/admin/roster", headers=admin, json=roster).json()["members_imported"] == 1
    assert client.post("/admin/roster", headers=admin, json=roster | {"members": [MEMBER, MEMBER]}).status_code == 409
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT carnet, section FROM roster").fetchall() == [(MEMBER["carnet"], "02")]

    rotation = client.post("/admin/teams/1/rotate-token", headers=admin)
    assert rotation.status_code == 200
    new_token = rotation.headers["X-Team-Token"]
    assert token != new_token and new_token not in rotation.text
    params = {"name": "submit_lab_results", "arguments": {
        "deliverable_id": "D1", "label": "=1+1", "commit_hash": "abc1234",
        "metrics_json": "{}", "is_ci_passing": True,
    }}
    assert rpc(client, "tools/call", params, token=token).status_code == 401
    assert rpc(client, "tools/call", params, token=new_token).json()["result"]["isError"] is False

    export = client.get("/admin/export?section=02", headers=admin)
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert [row["label"] for row in rows] == ["'=1+1"]
    assert "token_hash" not in rows[0]
    other = client.get("/admin/export?section=01", headers=admin)
    assert not list(csv.DictReader(io.StringIO(other.text)))

    log = grader_db.with_name("grader.log").read_text()
    assert "status=403" in log
    assert all(secret not in log for secret in (token, new_token, admin_token))
    assert all(json.loads(line)["timestamp"] for line in log.splitlines())


def test_register_team_limit_is_per_ip_and_only_applies_to_registration(client):
    for index in range(5):
        response = register(client, f"Equipo {index}", [{
            "github_user": f"alumna-{index}", "carnet": str(1234520 + index),
        }])
        assert response.status_code == 200
    rejected = register(client, "Otro equipo", [MEMBER])
    assert rejected.status_code == 429
    assert int(rejected.headers["Retry-After"]) in range(1, 61)
    assert rpc(client, "tools/list").status_code == 200


def test_sql_migrations_run_once(grader_db):
    database.init_db()
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall() == [
            ("001_initial_schema.sql",), ("002_teacher_operations.sql",),
        ]


def test_unexpected_admin_error_is_generic_and_log_redacts_secrets(client, grader_db, monkeypatch):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]

    def fail(team_id):
        raise RuntimeError(f'database failure Authorization: Bearer {token}, team_token="extra-secret"')

    monkeypatch.setattr(database, "rotate_team_token", fail)
    response = client.post("/admin/teams/1/rotate-token", headers={
        "Authorization": "Bearer admin-test-credential",
    })
    assert response.status_code == 500
    assert response.json() == {"error": "Error interno del servidor."}
    log = grader_db.with_name("grader.log").read_text()
    assert "database failure" in log and "[REDACTED]" in log
    assert token not in log and "extra-secret" not in log


@pytest.mark.parametrize(("modality", "members_count"), [
    ("individual", 1), ("pareja", 2), ("trio", 3),
])
def test_register_accepts_each_modality_and_unicode_team_name(client, grader_db, modality, members_count):
    members = [
        {"github_user": f"alumna-{index}", "carnet": str(1234520 + index)}
        for index in range(members_count)
    ]
    response = register(client, "Peña de Óscar", members, modality)
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert response.headers["X-Team-Token"]
    with closing(sqlite3.connect(grader_db)) as conn:
        team_name, stored_modality, stored_members = conn.execute(
            "SELECT team_name, modality, members FROM teams"
        ).fetchone()
    assert (team_name, stored_modality, json.loads(stored_members)) == (
        "Peña de Óscar", modality, members,
    )


@pytest.mark.parametrize(("modality", "members"), [
    pytest.param("trio", [
        {"github_user": f"alumna-{index}", "carnet": str(1234520 + index)}
        for index in range(4)
    ], id="cuarto-integrante"),
    pytest.param("pareja", [MEMBER], id="pareja-incompleta"),
    pytest.param("individual", [MEMBER | {"carnet": "1234A24"}], id="carne-invalido"),
    pytest.param("individual", [MEMBER | {"github_user": "-alumna"}], id="guion-inicial"),
    pytest.param("individual", [MEMBER | {"github_user": "Peña-Ávila"}], id="github-con-tildes-y-enie"),
])
def test_invalid_registration_creates_no_team(client, grader_db, modality, members):
    response = register(client, "Equipo", members, modality)
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert "X-Team-Token" not in response.headers
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM teams").fetchone() == (0,)


def submit(client, token, **overrides):
    arguments = {
        "deliverable_id": "D1", "label": "Revisión de diseño", "commit_hash": "abc1234",
        "metrics_json": "{}", "is_ci_passing": True,
    }
    return rpc(client, "tools/call", {
        "name": "submit_lab_results", "arguments": arguments | overrides,
    }, token=token)


@pytest.mark.parametrize("overrides", [
    pytest.param({"deliverable_id": "D11"}, id="D11"),
    pytest.param({"commit_hash": "ghijklm"}, id="commit-no-hex"),
    pytest.param({"metrics_json": json.dumps("x" * 65_536)}, id="metricas-ASCII-grandes"),
    pytest.param({"metrics_json": json.dumps("ñ" * 32_768, ensure_ascii=False)}, id="metricas-UTF8-grandes"),
    pytest.param({"metrics_json": '{"faltante":'}, id="JSON-invalido"),
])
def test_invalid_submission_writes_no_receipt(client, grader_db, overrides):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    response = submit(client, token, **overrides)
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone() == (0,)


def test_other_team_token_cannot_write_under_first_team(client, grader_db):
    first_token = register(client, "Equipo A", [MEMBER], section="01").headers["X-Team-Token"]
    second_token = register(client, "Equipo B", [
        {"github_user": "alumna-dos", "carnet": "1234624"},
    ], section="02").headers["X-Team-Token"]

    first = submit(client, first_token).json()["result"]["structuredContent"]
    assert first["section"] == "01" and first["attempt"] == 1
    spoofed = submit(client, second_token, team_id=1, section="01")
    assert spoofed.status_code == 200
    result = spoofed.json()["result"]
    if not result["isError"]:
        assert result["structuredContent"]["section"] == "02"
        assert result["structuredContent"]["attempt"] == 1

    second = submit(client, second_token).json()["result"]["structuredContent"]
    assert second["section"] == "02" and second["label"] == "Revisión de diseño"
    assert second["attempt"] == (1 if result["isError"] else 2)
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT team_id, attempt FROM submissions WHERE team_id = 1").fetchall() == [(1, 1)]


def test_section_export_cli(client, grader_db, monkeypatch, capsys):
    from tools.export_section import main

    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    assert submit(client, token).json()["result"]["isError"] is False
    monkeypatch.setattr("sys.argv", ["export_section", "02", "--db", str(grader_db)])
    main()
    assert [row["label"] for row in csv.DictReader(io.StringIO(capsys.readouterr().out))] == [
        "Revisión de diseño",
    ]

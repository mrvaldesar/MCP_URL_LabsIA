import asyncio
import csv
import hashlib
from uuid import uuid4
import io
import json
import sqlite3
from contextlib import AsyncExitStack, closing
from datetime import datetime, timedelta

import httpx
import pytest
from httpx import AsyncClient
from starlette.testclient import TestClient

from src import database, github, rate_limit
from src.server import create_server


@pytest.fixture
def grader_db(tmp_path, monkeypatch):
    monkeypatch.setattr(github, "_credential_alert", None)
    path = tmp_path / "grader.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setenv("GRADER_LOG_PATH", str(tmp_path / "grader.log"))
    monkeypatch.setenv("ADMIN_TOKEN", "admin-test-credential")
    monkeypatch.setenv("STRICT_ROSTER", "false")
    database.init_db()
    return path


def rpc(client, method, params=None, token=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    })


def register(client, team_name, members, modality="individual", section="02", **extra):
    return rpc(client, "tools/call", {
        "name": "register_team",
        "arguments": {
            "team_name": team_name, "section": section, "modality": modality,
            "members": members, "github_repository": f"lab-ia-s{section}-{uuid4().hex}", **extra,
        },
    })


MEMBER = {"github_user": "alumna-uno", "carnet": "1234524"}


@pytest.fixture
def github_api(monkeypatch):
    monkeypatch.setenv("GITHUB_COURSE_ORG", "curso")
    monkeypatch.setenv("GITHUB_API_TOKEN", "github-test-credential")
    requests = []
    commit_sha = "abc1234" + "0" * 33
    tag_sha = "1" * 40
    nested_sha = "2" * 40

    def handle(request):
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer github-test-credential"
        parts = request.url.path.split("/")
        if parts[1:] == ["orgs", "curso", "repos"]:
            return httpx.Response(200, json=[{"private": True}])
        assert parts[1:3] == ["repos", "curso"]
        if parts[3].startswith("lab-ia-"):
            if parts[3].endswith("sin-red"):
                raise httpx.ConnectError("sin red", request=request)
            if parts[3].endswith("inexistente"):
                return httpx.Response(404)
            if len(parts) == 4:
                return httpx.Response(200, json={"full_name": "/".join(parts[2:4]), "private": True})
            if parts[3].endswith("sin-permisos"):
                return httpx.Response(403)
            if parts[4] == "collaborators":
                return httpx.Response(200 if len(parts) == 5 else 404)
        requests.append(request)
        assert parts[3].startswith(("equipo-", "lab-ia-"))
        path = parts[4:]
        if len(path) == 3 and path[-1] == "status":
            return httpx.Response(200, json={"state": "success", "total_count": 1})
        if len(path) == 3 and path[-1] == "check-runs":
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if not path:
            return httpx.Response(200, json={"full_name": "/".join(parts[2:4])})
        if path == ["commits", "abc1235"]:
            return httpx.Response(404)
        if path == ["commits", "deadbee"]:
            return httpx.Response(200, json={"sha": "deadbee" + "0" * 33})
        if path in (["commits", "abc1234"], ["commits", commit_sha]):
            return httpx.Response(200, json={"sha": commit_sha})
        if path == ["git", "matching-refs", "tags"]:
            return httpx.Response(404) if parts[3] == "equipo-2" else httpx.Response(200, json=[])
        if path == ["git", "tags", nested_sha]:
            return httpx.Response(200, json={"object": {"type": "tag", "sha": tag_sha}})
        if path == ["git", "tags", tag_sha]:
            return httpx.Response(200, json={"object": {"type": "commit", "sha": commit_sha}})
        assert path[:3] == ["git", "ref", "tags"]
        label = "/".join(path[3:])
        if parts[3].endswith("-feliz"):
            return httpx.Response(200, json={"ref": f"refs/tags/{label}",
                                             "object": {"type": "commit", "sha": commit_sha}})
        if label == "D2":
            return httpx.Response(404)
        if label == "D3" and parts[3] == "equipo-3":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if label == "D3":
            return httpx.Response(401)
        if label == "D4":
            return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})
        target = {"type": "commit", "sha": commit_sha}
        if label == "D5":
            target["sha"] = "abc1235" + "0" * 33
        if label == "D7":
            return httpx.Response(502)
        if label == "D8":
            raise httpx.ConnectError("sin red", request=request)
        if label == "D9":
            return httpx.Response(200, content=b"{")
        if label == "D10":
            return httpx.Response(429)
        if label == "D6":
            target = {"type": "tag", "sha": nested_sha}
        return httpx.Response(200, json={"ref": f"refs/tags/{label}", "object": target})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    return requests


def assign_repository(grader_db, team_id=1):
    with closing(sqlite3.connect(grader_db)) as conn, conn:
        conn.execute("UPDATE teams SET github_repository = ? WHERE id = ?", (f"equipo-{team_id}", team_id))


@pytest.fixture
def client(grader_db, github_api):
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
        "deliverable_id": "D1", "label": "D1", "commit_hash": "abc1234",
        "metrics_json": "{}", "is_ci_passing": True,
    }
    params = {"name": "submit_lab_results", "arguments": arguments}
    for invalid in (None, token.rsplit(".", 1)[0] + "." + "0" * 64):
        response = rpc(client, "tools/call", params, token=invalid)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "Authorization: Bearer" in response.json()["error"]

    assign_repository(grader_db)
    first = rpc(client, "tools/call", params, token=token).json()["result"]["structuredContent"]
    arguments.update(is_ci_passing=False)
    second = rpc(client, "tools/call", params, token=token).json()["result"]["structuredContent"]
    assert first == {
        "receipt_id": 1, "deliverable_id": "D1", "label": "D1", "section": "02",
        "status": "received", "attempt": 1, "received_at_utc": first["received_at_utc"],
        "verification_status": "verificado", "verified_ci_status": "exitoso",
        "timing": "sin_plazo", "late_minutes": 0,
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
    submit_schema = next(tool["inputSchema"] for tool in tools if tool["name"] == "submit_lab_results")
    assert "64 KiB (65536 bytes) en UTF-8" in submit_schema["properties"]["metrics_json"]["description"]
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp"):
        assert client.get(path).status_code == 404


def test_health_without_authentication(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload.keys() == {"status", "timestamp", "stale_verification_count", "stale_verification_minutes"}
    assert payload["status"] == "ok"
    assert datetime.fromisoformat(payload["timestamp"]).utcoffset() == timedelta(0)


@pytest.mark.parametrize("change", ["missing", "extra", "different"])
def test_health_rejects_migration_mismatch(client, grader_db, change):
    with closing(sqlite3.connect(grader_db)) as conn, conn:
        if change in ("missing", "different"):
            conn.execute("DELETE FROM schema_migrations WHERE name = '001_initial_schema.sql'")
        if change in ("extra", "different"):
            conn.execute("INSERT INTO schema_migrations (name) VALUES ('999_unexpected.sql')")

    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


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
        "deliverable_id": "D1", "label": "D1", "commit_hash": "abc1234",
        "metrics_json": "{}", "is_ci_passing": True,
    }}
    assert rpc(client, "tools/call", params, token=token).status_code == 401
    assign_repository(grader_db)
    assert rpc(client, "tools/call", params, token=new_token).json()["result"]["isError"] is False

    # Las entregas históricas pueden contener etiquetas ajenas al formato actual.
    with closing(sqlite3.connect(grader_db)) as conn, conn:
        conn.execute("UPDATE submissions SET label = ?", ("=1+1",))
    export = client.get("/admin/export?section=02", headers=admin)
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert [row["label"] for row in rows] == ["'=1+1"]
    assert "token_hash" not in rows[0]
    other = client.get("/admin/export?section=01", headers=admin)
    assert not list(csv.DictReader(io.StringIO(other.text)))

    log = grader_db.with_name("grader.log").read_text()
    assert "HTTP GET /admin/export status=403" in log
    assert "section=02" not in log
    assert all(secret not in log for secret in (token, new_token, admin_token))
    assert all(json.loads(line)["timestamp"] for line in log.splitlines())


def test_register_team_limit_is_per_ip_and_only_applies_to_registration(grader_db, monkeypatch, request):
    monkeypatch.setenv("REGISTER_IP_LIMIT", "5")
    client = request.getfixturevalue("client")
    now = 1000
    monkeypatch.setattr(rate_limit, "monotonic", lambda: now)
    for index in range(5):
        response = register(client, f"Equipo {index}", [{
            "github_user": f"alumna-{index}", "carnet": str(1234520 + index),
        }])
        assert response.status_code == 200
    rejected = register(client, "Otro equipo", [MEMBER])
    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "600"
    assert rejected.json()["error"]["message"] == (
        "Has excedido el límite de peticiones. Por favor, espera 600 segundos antes de reintentar"
    )
    assert rpc(client, "tools/list").status_code == 200
    now += 599
    assert register(client, "Otro equipo", [MEMBER]).headers["Retry-After"] == "1"
    now += 1
    new_member = {"github_user": "otra-alumna", "carnet": "1234999"}
    assert register(client, "Otro equipo", [new_member]).headers.get("X-Team-Token")


def test_sql_migrations_run_once(grader_db):
    database.init_db()
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall() == [
            ("001_initial_schema.sql",), ("002_teacher_operations.sql",),
            ("003_team_github_repository.sql",), ("004_github_verification.sql",),
            ("005_delivery_windows.sql",), ("006_submission_team_snapshot.sql",), ("007_verification_retries.sql",),
            ("008_retry_schedule.sql",),
        ]


def test_unexpected_admin_error_is_generic_and_log_redacts_secrets(client, grader_db, monkeypatch):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]

    def fail(team_id, section):
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
        "deliverable_id": "D1", "label": "D1", "commit_hash": "abc1234",
        "metrics_json": "{}", "is_ci_passing": True,
    }
    return rpc(client, "tools/call", {
        "name": "submit_lab_results", "arguments": arguments | overrides,
    }, token=token)


@pytest.mark.parametrize("overrides", [
    pytest.param({"deliverable_id": "D11"}, id="D11"),
    pytest.param({"commit_hash": "ghijklm"}, id="commit-no-hex"),
    pytest.param({"commit_hash": "abc1234\n"}, id="commit-con-salto"),
    pytest.param({"metrics_json": json.dumps("x" * 65_536)}, id="metricas-ASCII-grandes"),
    pytest.param({"metrics_json": json.dumps("ñ" * 32_768, ensure_ascii=False)}, id="metricas-UTF8-grandes"),
    pytest.param({"metrics_json": '{"faltante":'}, id="JSON-invalido"),
])
def test_invalid_submission_writes_no_receipt(client, grader_db, github_api, overrides):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)
    response = submit(client, token, **overrides)
    assert github_api == []
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone() == (0,)


@pytest.mark.parametrize("label", ["D2", "d1", "D10", " D1", "D1 ", "D1\n", ""])
def test_label_must_match_deliverable_exactly(client, grader_db, github_api, label):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)
    result = submit(client, token, label=label).json()["result"]
    assert result["isError"] is True
    assert "label debe coincidir exactamente con deliverable_id (D1)" in result["content"][0]["text"]
    assert github_api == []
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone() == (0,)


def test_other_team_token_cannot_write_under_first_team(client, grader_db):
    first_token = register(client, "Equipo A", [MEMBER], section="01").headers["X-Team-Token"]
    second_token = register(client, "Equipo B", [
        {"github_user": "alumna-dos", "carnet": "1234624"},
    ], section="02").headers["X-Team-Token"]

    assign_repository(grader_db)
    assign_repository(grader_db, team_id=2)
    first = submit(client, first_token).json()["result"]["structuredContent"]
    assert first["section"] == "01" and first["attempt"] == 1
    spoofed = submit(client, second_token, team_id=1, section="01")
    assert spoofed.status_code == 200
    result = spoofed.json()["result"]
    if not result["isError"]:
        assert result["structuredContent"]["section"] == "02"
        assert result["structuredContent"]["attempt"] == 1

    second = submit(client, second_token).json()["result"]["structuredContent"]
    assert second["section"] == "02" and second["label"] == "D1"
    assert second["attempt"] == (1 if result["isError"] else 2)
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT team_id, attempt FROM submissions WHERE team_id = 1").fetchall() == [(1, 1)]


def test_section_export_cli(client, grader_db, monkeypatch, capsys):
    from tools.export_section import main

    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)
    assert submit(client, token).json()["result"]["isError"] is False
    monkeypatch.setattr("sys.argv", ["export_section", "02", "--db", str(grader_db)])
    main()
    assert [row["label"] for row in csv.DictReader(io.StringIO(capsys.readouterr().out))] == [
        "D1",
    ]


def test_github_annotated_tag_saves_verified_sha(client, grader_db, github_api):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)
    result = submit(client, token, deliverable_id="D6", label="D6").json()["result"]
    assert result["isError"] is False
    assert len(github_api) == 6
    assert github_api[0].url.raw_path.endswith(b"/git/ref/tags/D6")
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT commit_hash FROM submissions").fetchall() == [("abc1234" + "0" * 33,)]


@pytest.mark.parametrize(("label", "commit_hash", "message"), [
    ("D2", "abc1234", "Etiqueta no encontrada"),
    ("D5", "abc1235", "Commit inexistente"),
    ("D1", "deadbee", "apunta a otro commit"),
])
def test_github_rejection_writes_no_receipt(client, grader_db, label, commit_hash, message):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)
    result = submit(client, token, deliverable_id=label, label=label, commit_hash=commit_hash).json()["result"]
    assert result["isError"] is True
    assert message in result["content"][0]["text"]
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone() == (0,)


def test_github_verification_requires_assigned_repository(client, grader_db, github_api):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    with closing(sqlite3.connect(grader_db)) as conn, conn:
        conn.execute("UPDATE teams SET github_repository = NULL")
    result = submit(client, token).json()["result"]
    assert result["isError"] is True
    assert "no tiene un repositorio" in result["content"][0]["text"]
    assert github_api == []
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone() == (0,)


def test_submission_limit_blocks_eleventh_call_per_token(client, grader_db, github_api, monkeypatch):
    now = 1000
    monkeypatch.setattr(rate_limit, "monotonic", lambda: now)
    first_token = register(client, "Primer equipo", [MEMBER]).headers["X-Team-Token"]
    second_token = register(client, "Segundo equipo", [
        {"github_user": "otra-alumna", "carnet": "1234999"},
    ]).headers["X-Team-Token"]
    assign_repository(grader_db)
    assign_repository(grader_db, team_id=2)

    for attempt in range(1, 11):
        result = submit(client, first_token).json()["result"]
        assert result["structuredContent"]["attempt"] == attempt

    github_calls = len(github_api)
    rejected = submit(client, first_token)
    assert rejected.status_code == 200
    assert rejected.headers["Retry-After"] == "60"
    assert rejected.json() == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"isError": True, "content": [{
            "type": "text",
            "text": "Has excedido el límite de peticiones. Por favor, espera 60 segundos antes de reintentar",
        }]},
    }
    assert len(github_api) == github_calls
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone() == (10,)

    assert submit(client, second_token).json()["result"]["structuredContent"]["attempt"] == 1
    now += 59
    assert submit(client, first_token).headers["Retry-After"] == "1"
    now += 1
    assert submit(client, first_token).json()["result"]["structuredContent"]["attempt"] == 11


@pytest.mark.parametrize("strict", [None, "false", "true"])
def test_register_checks_roster_tuple_and_section(client, grader_db, monkeypatch, strict):
    if strict is None:
        monkeypatch.delenv("STRICT_ROSTER", raising=False)
    else:
        monkeypatch.setenv("STRICT_ROSTER", strict)

    other = {"github_user": "alumna-dos", "carnet": "1234624"}
    database.replace_roster("01", [(MEMBER["github_user"], MEMBER["carnet"])])
    database.replace_roster("02", [
        (other["github_user"], other["carnet"]),
        ("alumna-tres", "1234724"),
        ("alumna-cuatro", "1234824"),
    ])
    wrong_pair = {"github_user": "alumna-tres", "carnet": "1234824"}
    responses = [
        register(client, "Tupla incorrecta", [other, wrong_pair], modality="pareja"),
        register(client, "Sección incorrecta", [MEMBER]),
    ]
    for response in responses:
        assert response.json()["result"]["isError"] is (strict != "false")
        assert ("X-Team-Token" in response.headers) is (strict == "false")
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM teams").fetchone() == ((2 if strict == "false" else 0),)

    log = grader_db.with_name("grader.log").read_text()
    assert ('"level": "WARNING"' in log and "Registro fuera del padrón" in log) is (strict == "false")
    if strict != "false":
        assert "padrón de la sección" in responses[0].json()["result"]["content"][0]["text"]
        uppercase = MEMBER | {"github_user": MEMBER["github_user"].upper()}
        assert "X-Team-Token" in register(client, "Válido", [uppercase], section="01").headers


def test_admin_update_replaces_members_and_rotates_token(client, grader_db):
    departed = {"github_user": "alumna-dos", "carnet": "1234624"}
    old_token = register(client, "Equipo", [MEMBER, departed], modality="pareja").headers["X-Team-Token"]
    assign_repository(grader_db)
    path = "/admin/teams/1/update"
    admin = {"Authorization": "Bearer admin-test-credential"}
    payload = {"members": [MEMBER]}

    assert client.post(path, json=payload).status_code == 403
    assert client.post(path, json=payload, headers={"Authorization": f"Bearer {old_token}"}).status_code == 403
    assert client.post(path, json={"modality": "individual"}, headers=admin).status_code == 400
    assert client.post("/admin/teams/99/update", json=payload, headers=admin).status_code == 404

    response = client.post(path, json=payload, headers=admin)
    assert response.status_code == 200
    assert response.json() == {"team_id": 1}
    assert response.headers["Cache-Control"] == "no-store"
    new_token = response.headers["X-Team-Token"]
    assert new_token != old_token and new_token not in response.text
    assert submit(client, old_token).status_code == 401
    assert submit(client, new_token).json()["result"]["isError"] is False

    with closing(sqlite3.connect(grader_db)) as conn:
        modality, members = conn.execute("SELECT modality, members FROM teams WHERE id = 1").fetchone()
        assert modality == "individual" and json.loads(members) == [MEMBER]
        assert conn.execute("SELECT github_user, carnet FROM member_identities").fetchall() == [
            (MEMBER["github_user"], MEMBER["carnet"]),
        ]
    assert "X-Team-Token" in register(client, "Otro equipo", [departed]).headers


def test_admin_update_rolls_back_identity_conflict(client, grader_db):
    other = {"github_user": "alumna-dos", "carnet": "1234624"}
    old_token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    register(client, "Otro equipo", [other])
    response = client.post(
        "/admin/teams/1/update",
        json={"modality": "pareja", "members": [MEMBER, other]},
        headers={"Authorization": "Bearer admin-test-credential"},
    )
    assert response.status_code == 409
    assert "X-Team-Token" not in response.headers
    assert database.resolve_team_id(old_token) == 1
    with closing(sqlite3.connect(grader_db)) as conn:
        modality, members = conn.execute("SELECT modality, members FROM teams WHERE id = 1").fetchone()
        assert modality == "individual" and json.loads(members) == [MEMBER]
        assert conn.execute("SELECT COUNT(*) FROM member_identities").fetchone() == (2,)



def test_admin_update_accepts_modality_only(client, grader_db):
    old_token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    with closing(sqlite3.connect(grader_db)) as conn, conn:
        conn.execute("UPDATE teams SET modality = 'pareja' WHERE id = 1")

    response = client.post(
        "/admin/teams/1/update",
        json={"modality": "individual"},
        headers={"Authorization": "Bearer admin-test-credential"},
    )
    assert response.status_code == 200
    assert database.resolve_team_id(old_token) is None
    assert database.resolve_team_id(response.headers["X-Team-Token"]) == 1
    with closing(sqlite3.connect(grader_db)) as conn:
        modality, members = conn.execute("SELECT modality, members FROM teams WHERE id = 1").fetchone()
        assert modality == "individual" and json.loads(members) == [MEMBER]



def test_concurrent_team_registration_and_submission(grader_db, github_api, monkeypatch):
    monkeypatch.setenv("STRICT_ROSTER", "true")
    teams = {
        f"Equipo {number}": {"github_user": f"alumno-{number}", "carnet": f"{number:07d}"}
        for number in range(1, 21)
    }
    database.replace_roster("02", [(member["github_user"], member["carnet"]) for member in teams.values()])
    app = create_server().streamable_http_app()

    async def run_concurrent_requests():
        async with app.router.lifespan_context(app), AsyncExitStack() as stack:
            # Four client IPs keep all registrations within the production limit of five per IP.
            clients = [
                await stack.enter_async_context(AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=(f"192.0.2.{number}", 12345)),
                    base_url="http://localhost:8000",
                ))
                for number in range(1, 5)
            ]
            team_clients = dict(zip(teams, clients * 5, strict=True))
            registrations = await asyncio.gather(*(
                register(team_clients[name], name, [member]) for name, member in teams.items()
            ))
            tokens = {}
            for name, response in zip(teams, registrations, strict=True):
                assert response.status_code == 200, response.text
                assert response.json()["result"]["isError"] is False, response.text
                tokens[name] = response.headers["X-Team-Token"]
            assert len(set(tokens.values())) == 20

            # Repository assignment is a prerequisite for accepting submissions.
            with closing(sqlite3.connect(grader_db)) as conn, conn:
                conn.execute("UPDATE teams SET github_repository = 'equipo-' || id")

            submissions = await asyncio.gather(*(
                submit(team_clients[name], token) for name, token in tokens.items()
            ))
            receipts = {}
            for name, response in zip(teams, submissions, strict=True):
                assert response.status_code == 200, response.text
                result = response.json()["result"]
                assert result["isError"] is False, result
                receipt = result["structuredContent"]
                assert receipt["status"] == "received"
                assert receipt["section"] == "02"
                assert receipt["deliverable_id"] == receipt["label"] == "D1"
                assert receipt["attempt"] == 1
                receipts[name] = (receipt["receipt_id"], receipt["received_at_utc"])
            return receipts

    receipts = asyncio.run(run_concurrent_requests())

    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT COUNT(*), COUNT(DISTINCT team_name) FROM teams").fetchone() == (20, 20)
        assert conn.execute("SELECT COUNT(*) FROM member_identities").fetchone() == (20,)
        rows = conn.execute("""
            SELECT t.team_name, s.id, s.received_at_utc, s.deliverable_id, s.label, s.attempt,
                   s.commit_hash, s.declared_metrics_json, s.declared_is_ci_passing
            FROM submissions AS s JOIN teams AS t ON t.id = s.team_id
        """).fetchall()
        assert len(rows) == 20
        assert {row[0]: row[1:3] for row in rows} == receipts
        assert all(row[3:] == ("D1", "D1", 1, "abc1234" + "0" * 33, "{}", 1) for row in rows)
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert len(github_api) == 80


def test_empty_roster_startup_and_custom_carnet(client, grader_db, monkeypatch):
    assert "El padrón está vacío" in grader_db.with_name("grader.log").read_text()
    monkeypatch.setenv("CARNET_PATTERN", r"[A-Z][0-9]{8}")
    assert "X-Team-Token" not in register(client, "Formato antiguo", [MEMBER]).headers
    member = MEMBER | {"carnet": "A12345678"}
    assert "X-Team-Token" in register(client, "Formato configurado", [member]).headers


def test_campus_capacity_and_fabricated_carnets(client, monkeypatch):
    monkeypatch.delenv("STRICT_ROSTER")
    members = [{"github_user": f"alumna-{n}", "carnet": f"{n:07d}"} for n in range(40)]
    database.replace_roster("02", [(m["github_user"], m["carnet"]) for m in members])
    for n, member in enumerate(members):
        assert "X-Team-Token" in register(client, f"Equipo {n}", [member]).headers
    for n in range(100):
        response = register(client, "Inventado", [{"github_user": "inventado", "carnet": f"{n+100:07d}"}])
        assert "X-Team-Token" not in response.headers
        if n >= 40:
            assert response.status_code == 429


def test_repeated_failed_carnet(client, monkeypatch):
    monkeypatch.delenv("STRICT_ROSTER")
    for _ in range(5):
        assert "padrón" in register(client, "Inventado", [MEMBER]).text
    assert "Demasiados intentos" in register(client, "Inventado", [MEMBER]).text


def test_proxy_trust_boundary():
    from ipaddress import ip_network
    from src.auth import client_ip

    networks = (ip_network("10.0.0.0/24"),)
    headers = [(b"x-forwarded-for", b"198.51.100.99, 192.0.2.11, 10.0.0.9")]
    assert client_ip({"client": ("10.0.0.8", 1), "headers": headers}, networks) == "192.0.2.11"
    assert client_ip({"client": ("192.0.2.12", 1), "headers": headers}, networks) == "192.0.2.12"


@pytest.mark.parametrize(("repository", "expected"), [
    ("lab-ia-s02-valido", "no figura como colaborador"),
    ("lab-ia-s02-sin-permisos", "Equipo registrado."),
    ("lab-ia-s02-inexistente", "El repositorio no existe"),
    ("lab-ia-s02-sin-red", "Intenta más tarde"),
    ("lab-ia-s01-valido", "debe coincidir con la sección"),
    ("curso/lab-ia-s02-valido", "Usa el nombre corto"),
])
def test_repository_registration(client, grader_db, repository, expected):
    response = register(client, "Equipo", [MEMBER], github_repository=repository)
    assert expected in response.text
    if "X-Team-Token" in response.headers:
        assert database.get_team_repository(1) == repository
        other = {"github_user": "otra-alumna", "carnet": "1234999"}
        assert "ya está asignado" in register(client, "Otro", [other], github_repository=repository).text
    else:
        assert database.get_team_repository(1) is None


@pytest.mark.skipif(__import__("os").getenv("RUN_GITHUB_INTEGRATION") != "1", reason="requiere red pública")
def test_public_annotated_tag():
    async def check():
        async with github._client(public=True) as client:
            path = "/repos/git/git"
            ref = await github._get(client, path + "/git/ref/tags/v2.45.0")
            assert ref["object"]["type"] == "tag"
            tag = await github._get(client, path + "/git/tags/" + ref["object"]["sha"])
            sha = tag["object"]["sha"]
            assert await github.resolve_tag(client, path, "v2.45.0", sha[:12]) == sha
    asyncio.run(check())


@pytest.mark.parametrize("deliverable", ["D3", "D4", "D7", "D8", "D9", "D10"])
def test_github_failure_keeps_receipt(client, grader_db, deliverable):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)
    receipt = submit(client, token, deliverable_id=deliverable, label=deliverable).json()["result"]["structuredContent"]
    assert receipt["verification_status"] == "pendiente"
    assert receipt["verified_ci_status"] == "desconocido"
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT verification_status, commit_hash FROM submissions").fetchone() == ("pendiente", "abc1234")


def test_admin_retry_and_credential_health(client, grader_db, monkeypatch):
    from src import server
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)

    async def unavailable(*args):
        raise github.GitHubUnavailable()
    monkeypatch.setattr(server, "verify_submission", unavailable)
    receipt = submit(client, token).json()["result"]["structuredContent"]
    assert receipt["verification_status"] == "pendiente"
    headers = {"Authorization": "Bearer admin-test-credential"}
    path = f"/admin/submissions/{receipt['receipt_id']}/retry-verification"
    result = client.post(path, headers=headers)
    assert result.json() == {"receipt_id": 1, "verification_status": "verificado", "verified_ci_status": "exitoso", "verification_retry_count": 1}
    assert client.post(path, headers=headers).status_code == 404
    assert client.get("/admin/github/health", headers=headers).json() == {
        "github_credential_status": "valida", "github_app_repository_coverage": "no_aplica",
    }

@pytest.mark.parametrize(("bad", "hint"), [
    ({"deliverable_id": "D11"}, "Usa un entregable"),
    ({"commit_hash": "???secret-marker"}, "dígitos hexadecimales"),
    ({"metrics_json": 7}, "JSON válido"),
    ({"label": 7}, "etiqueta de texto"),
    ({"is_ci_passing": {}}, "true o false"),
])
def test_actionable_submission_validation(client, grader_db, bad, hint):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    response = submit(client, token, **bad).json()["result"]
    assert response["isError"]
    message = response["content"][0]["text"]
    assert hint in message
    assert "secret-marker" not in message
    assert "literal_error" not in message and "string_pattern_mismatch" not in message


def test_omitted_label_defaults_to_deliverable(client, grader_db):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)
    response = rpc(client, "tools/call", {"name": "submit_lab_results", "arguments": {
        "deliverable_id": "D1", "commit_hash": "abc1234", "metrics_json": "{}", "is_ci_passing": True,
    }}, token=token)
    assert response.json()["result"]["structuredContent"]["label"] == "D1"

@pytest.mark.parametrize(("opens", "closes", "expected"), [
    ("2000-01-01", "2001-01-01", "tardío"),
    ("2000-01-01", "2099-01-01", "a_tiempo"),
    ("2098-01-01", "2099-01-01", "sin_plazo"),
])
def test_delivery_windows_and_csv(client, grader_db, opens, closes, expected):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)
    headers = {"Authorization": "Bearer admin-test-credential"}
    window = {"section": "02", "deliverable_id": "D1", "opens_at": opens + "T00:00:00Z",
              "closes_at": closes + "T00:00:00Z"}
    assert client.put("/admin/windows", json=window, headers=headers).status_code == 200
    receipt = submit(client, token).json()["result"]["structuredContent"]
    assert receipt["timing"] == expected
    assert (receipt["late_minutes"] > 0) == (expected == "tardío")
    rows = csv.DictReader(io.StringIO(client.get("/admin/export?section=02", headers=headers).text))
    row = next(rows)
    assert row["timing"] == expected and int(row["late_minutes"]) == receipt["late_minutes"]
    assert (row["declared_is_ci_passing"], row["verified_ci_status"]) == ("1", "exitoso")

def test_teacher_scope_moving_team_and_historical_csv(client, grader_db, monkeypatch):
    teacher_token = "synthetic-teacher-token"
    monkeypatch.setenv("TEACHER_ADMIN_CREDENTIALS", json.dumps([
        {"id": "docente-01", "section": "01", "sha256": hashlib.sha256(teacher_token.encode()).hexdigest()},
    ]))
    teacher = {"Authorization": f"Bearer {teacher_token}"}
    admin = {"Authorization": "Bearer admin-test-credential"}
    first = register(client, "Uno", [MEMBER], section="01")
    register(client, "Dos", [{"github_user": "alumna-dos", "carnet": "0000002"}])
    assign_repository(grader_db)
    assert submit(client, first.headers["X-Team-Token"]).json()["result"]["structuredContent"]["section"] == "01"
    assert [team["id"] for team in client.get("/admin/teams", headers=teacher).json()["teams"]] == [1]
    assert client.get("/admin/teams?section=02", headers=teacher).status_code == 403
    assert client.get("/admin/export?section=02", headers=teacher).status_code == 403
    assert client.post("/admin/roster", headers=teacher, json={"section": "02", "members": []}).status_code == 403
    assert client.post("/admin/teams/2/rotate-token", headers=teacher).status_code == 404
    assert client.post("/admin/teams/2/update", headers=teacher, json={"modality": "individual"}).status_code == 404
    assert client.patch("/admin/teams/2/assignment", headers=teacher, json={"section": "01"}).status_code == 404
    path = "/admin/teams/1/assignment"
    assert client.patch(path, headers=teacher, json={"section": "02"}).status_code == 403
    assert client.patch(path, headers=teacher, json={"github_repository": "lab-ia-s01-uno"}).status_code == 200
    assert client.patch(path, headers=admin, json={"section": "02", "github_repository": "lab-ia-s02-uno"}).status_code == 200
    assert client.get("/admin/teams", headers=teacher).json()["teams"] == []
    csv_01 = client.get("/admin/export?section=01", headers=teacher).text
    assert next(csv.DictReader(io.StringIO(csv_01)))["section"] == "01"
    assert "actor=docente-01 acción=change_assignment recurso=1 estado=200" in grader_db.with_name("grader.log").read_text()

def test_hidden_contents_404_keeps_receipt(client, grader_db):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    with closing(sqlite3.connect(grader_db)) as conn, conn:
        conn.execute("UPDATE teams SET github_repository = 'equipo-2'")
    result = submit(client, token, deliverable_id="D2", label="D2").json()["result"]
    assert result["structuredContent"]["verification_status"] == "pendiente"


def test_registration_global_limit_spans_client_ips(grader_db, monkeypatch, github_api):
    monkeypatch.setenv("REGISTER_GLOBAL_LIMIT", "1")
    app = create_server().streamable_http_app()
    with TestClient(app, client=("192.0.2.1", 12345), base_url="http://localhost:8000") as first:
        assert "X-Team-Token" in register(first, "Primero", [MEMBER]).headers
        second = TestClient(app, client=("192.0.2.2", 12345), base_url="http://localhost:8000")
        assert register(second, "Segundo", [MEMBER]).status_code == 429


def test_scoped_window_reopening(client, grader_db, monkeypatch):
    token = "synthetic-reopen-token"
    monkeypatch.setenv("TEACHER_ADMIN_CREDENTIALS", json.dumps([
        {"id": "docente-01", "section": "01", "sha256": hashlib.sha256(token.encode()).hexdigest()},
    ]))
    headers = {"Authorization": f"Bearer {token}"}
    window = {"section": "02", "deliverable_id": "D1", "opens_at": "2000-01-01T00:00:00Z",
              "closes_at": "2001-01-01T00:00:00Z"}
    assert client.put("/admin/windows", json=window, headers=headers).status_code == 403
    window["section"] = "01"
    assert client.put("/admin/windows", json=window, headers=headers).status_code == 200
    assert client.put("/admin/windows", json=window | {"closes_at": "2099-01-01T00:00:00Z"},
                      headers=headers).status_code == 200
    assert client.put("/admin/windows", json=window | {"closes_at": "1999-01-01T00:00:00Z"},
                      headers=headers).status_code == 400
    assert client.put("/admin/windows", json=window | {"closes_at": "2099-01-01"},
                      headers=headers).status_code == 400


def test_backup_and_restore_preserves_synthetic_team_and_receipt(client, grader_db, tmp_path, monkeypatch):
    from shutil import copyfile
    from tools import backup_db

    token = register(client, "Copia sintética", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)
    assert submit(client, token).json()["result"]["structuredContent"]["receipt_id"] == 1
    bucket = {}

    def mock_transfer(source, target, *, upload=False):
        if upload:
            bucket[target] = tmp_path / "object.db"
            copyfile(source, bucket[target])
        else:
            copyfile(bucket[source], target)

    monkeypatch.setattr(backup_db, "transfer", mock_transfer)
    uri = backup_db.backup(grader_db, "s3://backup-sintetico/grader")
    restored = tmp_path / "restored.db"
    backup_db.restore(uri, restored)
    monkeypatch.setattr(database, "DB_PATH", restored)
    assert database.check_health()
    assert database.resolve_team_id(token) == 1
    with closing(sqlite3.connect(restored)) as conn:
        assert conn.execute("SELECT id, verification_status FROM submissions").fetchall() == [(1, "verificado")]
    with pytest.raises(FileExistsError):
        backup_db.restore(uri, restored)


def test_backup_cli_s3_arguments_and_transfer(tmp_path, monkeypatch, capsys):
    import sys
    from tools import backup_db

    calls = []
    monkeypatch.setattr(backup_db.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd))
    snapshot = tmp_path / "snapshot.db"
    backup_db.transfer(snapshot, "s3://example/snapshot.db", upload=True)
    assert calls[0] == ["aws", "s3", "cp", str(snapshot), "s3://example/snapshot.db",
                        "--only-show-errors", "--sse", "AES256"]
    monkeypatch.setattr(backup_db, "backup", lambda db, destination: destination + "/snapshot.db")
    monkeypatch.setattr(sys, "argv", ["backup_db", "backup", "--destination", "s3://example"])
    backup_db.main()
    assert capsys.readouterr().out.strip() == "s3://example/snapshot.db"


def test_reference_matches_live_tools_list(client, capsys):
    from pathlib import Path
    from tools.generate_tool_reference import main

    reference = Path(__file__).with_name("docs") / "tools-list.json"
    expected = json.loads(reference.read_text())
    assert rpc(client, "tools/list").json() == {key: value for key, value in expected.items()
                                                if key != "interface_version"}
    main()
    assert json.loads(capsys.readouterr().out) == expected


def test_streamable_http_over_tcp(grader_db, github_api):
    import socket
    from threading import Thread
    from time import monotonic, sleep

    import uvicorn

    app = create_server().streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(app, proxy_headers=False, log_level="error"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 5
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.01)
            assert server.started
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                assert client.get("/health").status_code == 200
                listed = rpc(client, "tools/list")
                assert {item["name"] for item in listed.json()["result"]["tools"]} == {
                    "register_team", "submit_lab_results",
                }
                registered = register(client, "Equipo HTTP", [MEMBER])
                assert registered.headers.get("X-Team-Token")
                assert "X-Team-Token" not in registered.text
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("section", ["01", "02"])
def test_delivery_matrix_http(client, grader_db, section):
    token = register(client, "Equipo matriz", [MEMBER], section=section).headers["X-Team-Token"]
    assign_repository(grader_db)
    expected = {
        "D1": "verificado", "D2": "Etiqueta no encontrada", "D3": "pendiente",
        "D4": "pendiente", "D5": "apunta a otro commit", "D6": "verificado",
        "D7": "pendiente", "D8": "pendiente", "D9": "pendiente", "D10": "pendiente",
    }
    for deliverable, outcome in expected.items():
        result = submit(client, token, deliverable_id=deliverable, label=deliverable).json()["result"]
        if outcome in ("verificado", "pendiente"):
            receipt = result["structuredContent"]
            assert receipt["section"] == section
            assert receipt["deliverable_id"] == deliverable
            assert receipt["verification_status"] == outcome
        else:
            assert result["isError"] and outcome in result["content"][0]["text"]


def test_upgrade_from_003_preserves_legacy_receipt_and_team_token(tmp_path, monkeypatch):
    from pathlib import Path

    original_migrations = database.MIGRATIONS_DIR
    legacy_migrations = tmp_path / "legacy-migrations"
    legacy_migrations.mkdir()
    for file in sorted(original_migrations.glob("*.sql"))[:3]:
        (legacy_migrations / file.name).write_bytes(file.read_bytes())
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(database, "MIGRATIONS_DIR", legacy_migrations)
    monkeypatch.setenv("STRICT_ROSTER", "false")
    database.init_db()
    token = database.register_team_db("Equipo anterior", "01", "individual", [MEMBER], "lab-ia-s01-viejo")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("""
            INSERT INTO submissions (team_id, deliverable_id, label, commit_hash, declared_metrics_json,
                declared_is_ci_passing, received_at_utc, attempt)
            VALUES (1, 'D1', 'D1', 'abc1234', '{}', 1, '2026-01-01T00:00:00+00:00', 1)
        """)
    monkeypatch.setattr(database, "MIGRATIONS_DIR", Path(original_migrations))
    database.init_db()
    assert database.resolve_team_id(token) == 1
    assert database.check_health()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("""
            SELECT id, commit_hash, verification_status, timing, received_section, received_team_name
            FROM submissions
        """).fetchall() == [(1, "abc1234", "sin_verificar", "sin_plazo", "01", "Equipo anterior")]


def test_retry_inconsistency_and_scope(client, grader_db, monkeypatch):
    token = register(client, "Pendiente", [MEMBER]).headers["X-Team-Token"]
    assign_repository(grader_db)
    result = submit(client, token, deliverable_id="D7", label="D7").json()["result"]
    assert result["structuredContent"]["verification_status"] == "pendiente"
    path = "/admin/submissions/1/retry-verification"
    token_01 = "synthetic-section-01"
    monkeypatch.setenv("TEACHER_ADMIN_CREDENTIALS", json.dumps([
        {"id": "docente-01", "section": "01", "sha256": hashlib.sha256(token_01.encode()).hexdigest()},
    ]))
    assert client.post(path, headers={"Authorization": f"Bearer {token_01}"}).status_code == 404
    admin = {"Authorization": "Bearer admin-test-credential"}
    assert client.post(path, headers=admin).status_code == 202

    async def mismatch(*args):
        raise github.GitHubVerificationError("La etiqueta apunta a otro commit.")
    monkeypatch.setattr(github, "verify_submission", mismatch)
    with closing(sqlite3.connect(grader_db)) as conn, conn:
        conn.execute("UPDATE submissions SET verification_retry_next_at = 0")
    assert client.post(path, headers=admin).json()["verification_status"] == "inconsistente"
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT attempt, verification_status FROM submissions").fetchall() == [(1, "inconsistente")]

def test_registration_requires_repository(client, grader_db):
    response = rpc(client, "tools/call", {"name": "register_team", "arguments": {
        "team_name": "Sin repositorio", "section": "02", "modality": "individual", "members": [MEMBER],
    }})
    assert response.json()["result"]["isError"]
    assert "Indica github_repository" in response.text
    assert "X-Team-Token" not in response.headers
    assert database.list_teams(None) == []


def test_failed_carnet_limit_does_not_cross_ips(grader_db, github_api, monkeypatch):
    monkeypatch.delenv("STRICT_ROSTER")
    monkeypatch.setenv("REGISTER_CARNET_FAILURE_LIMIT", "1")
    database.replace_roster("02", [(MEMBER["github_user"], MEMBER["carnet"])])
    app = create_server().streamable_http_app()
    with TestClient(app, client=("192.0.2.10", 12345), base_url="http://localhost:8000") as other:
        wrong = MEMBER | {"github_user": "equivocado"}
        assert "padrón" in register(other, "Equivocado", [wrong]).text
        assert "Demasiados intentos" in register(other, "Equivocado", [MEMBER]).text
        student = TestClient(app, client=("192.0.2.11", 12345), base_url="http://localhost:8000")
        assert "X-Team-Token" in register(student, "Estudiante", [MEMBER]).headers


def test_periodic_retries_stop_at_limit_and_report_aged_receipts(grader_db, github_api, monkeypatch):
    from time import monotonic, sleep

    monkeypatch.setenv("GITHUB_RETRY_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("GITHUB_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("GITHUB_PENDING_ALERT_MINUTES", "3")
    with TestClient(create_server().streamable_http_app(), base_url="http://localhost:8000") as client:
        token = register(client, "Equipo pendiente", [MEMBER]).headers["X-Team-Token"]
        response = submit(client, token, deliverable_id="D7", label="D7")
        assert response.json()["result"]["structuredContent"]["verification_status"] == "pendiente"
        with closing(sqlite3.connect(grader_db)) as conn, conn:
            conn.execute("UPDATE submissions SET received_at_utc = datetime('now', '-4 minutes')")
        assert client.get("/health").json()["stale_verification_count"] == 1
        assert client.get("/health").json()["stale_verification_minutes"] == 3
        deadline = monotonic() + 8
        log_path = grader_db.with_name("grader.log")
        while "agotó sus reintentos" not in log_path.read_text() and monotonic() < deadline:
            sleep(0.02)
        log = log_path.read_text()
        assert "por más de 3 minutos cantidad=1" in log
        assert "actor=reintento_automático recibo=1 reintento=2 estado=pendiente" in log
        headers = {"Authorization": "Bearer admin-test-credential"}
        assert client.post("/admin/submissions/1/retry-verification", headers=headers).status_code == 409
        row = next(csv.DictReader(io.StringIO(client.get("/admin/export?section=02", headers=headers).text)))
        assert (row["verification_status"], row["verified_ci_status"], row["verification_retry_count"]) == (
            "pendiente", "desconocido", "2",
        )
        assert database.pending_verification_ids(2) == []


@pytest.mark.parametrize(("repository_id", "deliverable", "alert"), [
    (1, "D3", 401), (3, "D3", 403), (1, "D4", None),
])
def test_github_credentials_alert_and_keep_receipts(client, grader_db, repository_id, deliverable, alert):
    token = register(client, "Equipo", [MEMBER]).headers["X-Team-Token"]
    with closing(sqlite3.connect(grader_db)) as conn, conn:
        conn.execute("UPDATE teams SET github_repository = ?", (f"equipo-{repository_id}",))
    receipt = submit(client, token, deliverable_id=deliverable, label=deliverable).json()["result"]["structuredContent"]
    assert receipt["verification_status"] == "pendiente"
    log = grader_db.with_name("grader.log").read_text()
    if alert is None:
        assert "Credencial de GitHub inválida" not in log
    else:
        assert f"Credencial de GitHub inválida o con permisos insuficientes estado={alert}" in log


def test_github_app_signs_and_reuses_installation_token(tmp_path, monkeypatch):
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "app.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    monkeypatch.setenv("GITHUB_COURSE_ORG", "curso")
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(path))
    monkeypatch.setenv("GITHUB_API_TOKEN", "synthetic-unused-pat")
    monkeypatch.setattr(github, "_installation_token", None)
    requests = []

    def handle(request):
        requests.append(request.url.path)
        bearer = request.headers["authorization"].removeprefix("Bearer ")
        if request.method == "POST":
            assert request.url.path == "/app/installations/456/access_tokens"
            assert jwt.decode(bearer, key.public_key(), algorithms=["RS256"])["iss"] == "123"
            return httpx.Response(201, json={"token": "synthetic-installation-token", "expires_at": "2099-01-01T00:00:00Z"})
        assert bearer == "synthetic-installation-token"
        return httpx.Response(200, json={"repositories": [{"full_name": "curso/lab-ia-s01-piloto", "private": True}]})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(handle), **kwargs,
    ))

    async def check():
        assert await github.credential_status() == "valida"
        assert await github.credential_status() == "valida"

    asyncio.run(check())
    assert requests == ["/app/installations/456/access_tokens", "/installation/repositories", "/installation/repositories"]


# Huellas congeladas. Agrega una entrada al publicar otra versión.
INTERFACE_SIGNATURES = {
    1: "a12a0632f38d02e2f21cd0c3f15b9d180c69755b1a45bbd2463e410adaec9ebc",
    2: "0aad53162ff1d9e541f7c1ee46ca7d33d0c7b2a4261727db110388ea43aa94fb",
}


def test_tool_signature_requires_a_new_interface_version(client):
    from pathlib import Path

    reference = json.loads(Path("docs/tools-list.json").read_text())
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    fields = ("name", "inputSchema", "outputSchema")
    signature = [{key: tool[key] for key in fields if key in tool}
                 for tool in sorted(tools, key=lambda tool: tool["name"])]
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    version = reference["interface_version"]
    assert version == max(INTERFACE_SIGNATURES)
    assert fingerprint == INTERFACE_SIGNATURES[version], (
        f"Cambió la firma MCP. Publica una nueva interface_version con huella {fingerprint}."
    )


@pytest.mark.parametrize("section", ["01", "02"])
@pytest.mark.parametrize("sha_length", [7, 40])
def test_happy_delivery_matrix_and_csv(client, github_api, monkeypatch, section, sha_length):
    from datetime import timezone

    from src import server

    class FrozenTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 13, tzinfo=timezone.utc)

    monkeypatch.setattr(server, "datetime", FrozenTime)
    full_sha = "abc1234" + "0" * 33
    registration = register(client, "Equipo matriz", [MEMBER], section=section,
                            github_repository=f"lab-ia-s{section}-feliz")
    headers = {"Authorization": "Bearer admin-test-credential"}
    window = {"section": section, "deliverable_id": "D1",
              "opens_at": "2026-01-01T08:00:00Z", "closes_at": "2026-01-01T12:00:00Z"}
    assert client.put("/admin/windows", headers=headers, json=window).status_code == 200

    for number in range(1, 11):
        deliverable = f"D{number}"
        payload = submit(client, registration.headers["X-Team-Token"], deliverable_id=deliverable,
                         label=deliverable, commit_hash=full_sha[:sha_length]).json()["result"]
        assert payload["isError"] is False
        receipt = payload["structuredContent"]
        assert (receipt["status"], receipt["section"], receipt["label"]) == ("received", section, deliverable)
        assert (receipt["verification_status"], receipt["verified_ci_status"]) == ("verificado", "exitoso")
        assert (receipt["timing"], receipt["late_minutes"]) == (
            ("tardío", 60) if number == 1 else ("sin_plazo", 0)
        )
    response = client.get(f"/admin/export?section={section}", headers=headers)
    csv_rows = list(csv.DictReader(io.StringIO(response.text)))
    assert [row["label"] for row in csv_rows] == [f"D{number}" for number in range(1, 11)]
    assert all(row["commit_hash"] == full_sha and row["section"] == section for row in csv_rows)
    fields = ("section", "deliverable_id", "timing", "late_minutes", "verification_status",
              "verified_ci_status", "verification_retry_count")
    excerpt = [tuple(row[field] for field in fields) for row in csv_rows[:2]]
    assert excerpt == [
        (section, "D1", "tardío", "60", "verificado", "exitoso", "0"),
        (section, "D2", "sin_plazo", "0", "verificado", "exitoso", "0"),
    ]


def test_manual_and_automatic_retries_cannot_claim_same_receipt(client, grader_db, monkeypatch):
    from src import verification

    token = register(client, "Equipo pendiente", [MEMBER]).headers["X-Team-Token"]
    assert submit(client, token, deliverable_id="D7", label="D7").json()["result"]["structuredContent"][
        "verification_status"
    ] == "pendiente"

    async def check():
        started = asyncio.Event()
        release = asyncio.Event()

        async def github_unavailable(*args):
            started.set()
            await release.wait()
            raise github.GitHubUnavailable()

        monkeypatch.setattr(github, "verify_submission", github_unavailable)
        automatic = asyncio.create_task(verification.retry(1, None, "reintento_automático"))
        await asyncio.wait_for(started.wait(), timeout=5)
        try:
            assert await verification.retry(1, "02", "docente-02") == ("en_curso", None)
        finally:
            release.set()
        state, receipt = await asyncio.wait_for(automatic, timeout=5)
        assert state == "pendiente"
        assert receipt["verification_retry_count"] == 1

    asyncio.run(check())
    log = grader_db.with_name("grader.log").read_text()
    assert "Reintento de verificación iniciado actor=reintento_automático recibo=1 reintento=1" in log


def test_retry_backoff_headers_window_and_scoped_reset(client, grader_db, monkeypatch):
    from time import time

    from src import verification

    monkeypatch.setenv("GITHUB_RETRY_WINDOW_HOURS", "1")
    token = register(client, "Reintentos", [MEMBER]).headers["X-Team-Token"]
    receipt = submit(client, token, deliverable_id="D7", label="D7").json()["result"]["structuredContent"]
    assert receipt["verification_status"] == "pendiente"
    now = int(time())
    response = httpx.Response(429, headers={"Retry-After": "120", "X-RateLimit-Reset": str(now + 180)})
    assert github.retry_at(response) == now + 180
    from email.utils import formatdate
    assert github.retry_at(httpx.Response(503, headers={"Retry-After": formatdate(now + 90, usegmt=True)})) == now + 90

    async def unavailable(*args):
        raise github.GitHubUnavailable(retry_at=now + 180)

    def next_at():
        with closing(sqlite3.connect(grader_db)) as conn:
            return conn.execute("SELECT verification_retry_next_at FROM submissions").fetchone()[0]

    monkeypatch.setattr(github, "verify_submission", unavailable)
    assert asyncio.run(verification.retry(1, "02", "docente"))[1]["verification_retry_count"] == 1
    assert next_at() == now + 180
    assert asyncio.run(verification.retry(1, "02", "docente"))[0] == "programado"
    with closing(sqlite3.connect(grader_db)) as conn, conn:
        conn.execute("UPDATE submissions SET verification_retry_next_at = 0")
    monkeypatch.setattr(verification, "retry_interval", lambda: 100)
    assert asyncio.run(verification.retry(1, "02", "docente"))[1]["verification_retry_count"] == 2
    assert next_at() >= now + 200
    with closing(sqlite3.connect(grader_db)) as conn, conn:
        conn.execute("UPDATE submissions SET received_at_utc = datetime('now', '-2 hours')")
    assert asyncio.run(verification.retry(1, "02", "docente"))[0] == "agotado"
    assert database.pending_verification_ids(5, 3600) == []
    path = "/admin/submissions/1/reset-verification"
    headers = {"Authorization": "Bearer admin-test-credential"}
    assert client.post(path, headers=headers).json() == {"receipt_id": 1, "verification_retry_count": 0}
    assert next_at() >= now + 200
    assert asyncio.run(verification.retry(1, "02", "docente"))[0] == "programado"
    assert "actor=superadmin acción=reset_verification recurso=1" in grader_db.with_name("grader.log").read_text()


def test_retry_credential_failure_refunds_and_pauses(client, monkeypatch):
    from src import verification

    token = register(client, "Credencial", [MEMBER]).headers["X-Team-Token"]
    submit(client, token, deliverable_id="D7", label="D7")
    calls = []

    async def invalid(*args):
        calls.append(args)
        github._credential_error(401)

    monkeypatch.setattr(github, "verify_submission", invalid)
    assert asyncio.run(verification.retry(1, "02", "docente"))[1]["verification_retry_count"] == 0
    assert asyncio.run(verification.retry(1, "02", "docente")) == ("programado", (0, 0))
    assert len(calls) == 1


@pytest.mark.parametrize(("selection", "coverage"), [("all", "todos"), ("selected", "seleccionados")])
def test_app_coverage_uses_organization_installation_metadata(client, monkeypatch, grader_db, selection, coverage):
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setattr(github, "_app_assertion", lambda config: "synthetic-app-jwt")

    async def status():
        return "valida"

    async def request(http_client, path):
        assert path == "/orgs/curso/installation"
        assert http_client.headers["authorization"] == "Bearer synthetic-app-jwt"
        return httpx.Response(200, json={"id": 456, "repository_selection": selection})

    monkeypatch.setattr(github, "credential_status", status)
    monkeypatch.setattr(github, "_request", request)
    headers = {"Authorization": "Bearer admin-test-credential"}
    result = client.get("/admin/github/health", headers=headers)
    assert result.json()["github_app_repository_coverage"] == coverage
    if selection == "selected":
        assert "debe instalarse para todos" in grader_db.with_name("grader.log").read_text()

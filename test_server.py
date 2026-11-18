import asyncio
import csv
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
def github_api(monkeypatch):
    monkeypatch.setenv("GITHUB_COURSE_ORG", "curso")
    monkeypatch.setenv("GITHUB_API_TOKEN", "github-test-credential")
    requests = []
    commit_sha = "abc1234" + "0" * 33
    tag_sha = "1" * 40

    def handle(request):
        requests.append(request)
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer github-test-credential"
        parts = request.url.path.split("/")
        assert parts[1:3] == ["repos", "curso"]
        assert parts[3] in {f"equipo-{team_id}" for team_id in range(1, 21)}
        path = parts[4:]
        if path == ["commits", "abc1235"]:
            return httpx.Response(404)
        if path == ["commits", "abc1234"]:
            return httpx.Response(200, json={"sha": commit_sha})
        if path == ["git", "tags", tag_sha]:
            return httpx.Response(200, json={"object": {"type": "commit", "sha": commit_sha}})
        assert path[:3] == ["git", "ref", "tags"]
        label = "/".join(path[3:])
        if label == "D2":
            return httpx.Response(404)
        if label == "D3":
            return httpx.Response(401)
        if label == "D4":
            return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})
        target = {"type": "commit", "sha": commit_sha}
        if label == "D5":
            target["sha"] = "abc1235" + "0" * 33
        if label == "D6":
            target = {"type": "tag", "sha": tag_sha}
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
    assert payload.keys() == {"status", "timestamp"}
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


def test_register_team_limit_is_per_ip_and_only_applies_to_registration(client, monkeypatch):
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
            ("003_team_github_repository.sql",),
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
    assert len(github_api) == 3
    assert github_api[0].url.raw_path.endswith(b"/git/ref/tags/D6")
    with closing(sqlite3.connect(grader_db)) as conn:
        assert conn.execute("SELECT commit_hash FROM submissions").fetchall() == [("abc1234" + "0" * 33,)]


@pytest.mark.parametrize(("label", "commit_hash", "message"), [
    ("D2", "abc1234", "no existe"),
    ("D5", "abc1235", "no existe"),
    ("D3", "abc1234", "Falló la autenticación"),
    ("D4", "abc1234", "Intenta más tarde"),
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
    assert len(github_api) == 40


def test_empty_roster_startup_and_custom_carnet(client, grader_db, monkeypatch):
    assert "El padrón está vacío" in grader_db.with_name("grader.log").read_text()
    monkeypatch.setenv("CARNET_PATTERN", r"[A-Z][0-9]{8}")
    assert "X-Team-Token" not in register(client, "Formato antiguo", [MEMBER]).headers
    member = MEMBER | {"carnet": "A12345678"}
    assert "X-Team-Token" in register(client, "Formato configurado", [member]).headers

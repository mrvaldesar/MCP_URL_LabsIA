import asyncio

import httpx
import pytest

from src import database
from tools import startup_checks


@pytest.fixture
def configured_server(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-test-secret")
    monkeypatch.setenv("GITHUB_COURSE_ORG", "course")
    monkeypatch.setenv("GITHUB_API_TOKEN", "github-test-secret")
    database.init_db()


@pytest.mark.parametrize("status_code", [200, 401])
def test_report_checks_an_assigned_repository(
    configured_server, monkeypatch, capsys, status_code
):
    team_token = database.register_team_db(
        "Equipo", "01", "individual", [{"github_user": "alumna", "carnet": "1234567"}]
    )
    team_id = database.resolve_team_id(team_token)
    with database._connect() as conn:
        conn.execute("UPDATE teams SET github_repository = 'lab' WHERE id = ?", (team_id,))

    def github(request):
        assert request.url.path == "/repos/course/lab/commits"
        assert request.url.params["per_page"] == "1"
        assert request.headers["authorization"] == "Bearer github-test-secret"
        return httpx.Response(status_code)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client_class(
        transport=httpx.MockTransport(github), **options
    ))

    asyncio.run(startup_checks.report_startup(None))
    output = capsys.readouterr().out
    assert "[OK] SQLite" in output and "[OK] Administración" in output
    assert ("ONLINE" in output) == (status_code == 200)
    assert ("HTTP 401" in output) == (status_code == 401)
    assert "test-secret" not in output


def test_report_stays_available_without_secrets(configured_server, monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_TOKEN")
    monkeypatch.delenv("GITHUB_API_TOKEN")
    asyncio.run(startup_checks.report_startup(None))
    output = capsys.readouterr().out
    assert "[OK] SQLite" in output
    assert "[PENDIENTE] Administración" in output and "[PENDIENTE] GitHub" in output
    assert "ONLINE" not in output

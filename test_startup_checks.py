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
def test_report_checks_an_explicit_repository(
    configured_server, monkeypatch, capsys, status_code
):
    def github(request):
        assert request.url.path == "/repos/course/lab/commits"
        assert request.url.params["per_page"] == "1"
        assert request.headers["authorization"] == "Bearer github-test-secret"
        return httpx.Response(status_code)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client_class(
        transport=httpx.MockTransport(github), **options
    ))

    asyncio.run(startup_checks.report_startup("lab"))
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


def test_github_deadline_reports_error_without_stopping_startup(
    configured_server, monkeypatch, capsys
):
    async def github(request):
        await asyncio.sleep(10)

    async def short_deadline(awaitable, timeout):
        assert timeout == 6.0
        return await wait_for(awaitable, timeout=0.01)

    wait_for = asyncio.wait_for
    client_class = httpx.AsyncClient
    monkeypatch.setattr(asyncio, "wait_for", short_deadline)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client_class(
        transport=httpx.MockTransport(github), **options
    ))

    asyncio.run(startup_checks.report_startup("lab"))
    output = capsys.readouterr().out
    assert "[ERROR] GitHub" in output
    assert "Servidor en ejecución" in output
    assert "ONLINE" not in output



@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, [{"name": "lab"}], "[OK] GitHub"),
        (200, [], "[PENDIENTE] GitHub"),
        (200, {"message": "unexpected"}, "[ERROR] GitHub"),
        (401, {"message": "Bad credentials"}, "[ERROR] GitHub"),
    ],
)
def test_report_checks_organization_without_assigned_repositories(
    configured_server, monkeypatch, capsys, status_code, payload, expected
):
    def github(request):
        assert request.url.path == "/orgs/course/repos"
        assert request.url.params["per_page"] == "1"
        assert request.headers["authorization"] == "Bearer github-test-secret"
        return httpx.Response(status_code, json=payload)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client_class(
        transport=httpx.MockTransport(github), **options
    ))
    asyncio.run(startup_checks.report_startup(None))
    output = capsys.readouterr().out
    assert expected in output
    assert ("ONLINE" in output) == (expected == "[OK] GitHub")
    assert "github-test-secret" not in output

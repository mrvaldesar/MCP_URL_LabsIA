import json
import sys

import pytest

from tools import local_server, startup_checks


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    path = tmp_path / "grader-settings.json"
    monkeypatch.setattr(local_server, "SETTINGS_PATH", path)
    return path


def run_wizard(monkeypatch, answers, hidden_answers=()):
    answers = iter(answers)
    hidden_answers = iter(hidden_answers)
    monkeypatch.setattr(sys, "argv", ["local_server", "configure"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(local_server, "getpass", lambda _: next(hidden_answers))
    return local_server.main()


def test_configure_generates_admin_token_and_saves_settings(
    settings_path, monkeypatch, capsys
):
    assert run_wizard(
        monkeypatch, ["invalid", "1", "1", "1", "invalid/org", "course-org", "1", "1", "course.log"],
        ["github-test-secret"],
    ) == 0

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    token = settings.pop("ADMIN_TOKEN")
    assert len(token) >= 32
    assert settings == {
        "GITHUB_COURSE_ORG": "course-org",
        "GITHUB_API_TOKEN": "github-test-secret",
        "STRICT_ROSTER": "true",
        "GRADER_LOG_PATH": "course.log",
    }
    output = capsys.readouterr().out
    assert f"Token docente generado: {token}" in output
    assert "github-test-secret" not in output


def test_configure_accepts_existing_token_and_keeps_other_settings(settings_path, monkeypatch):
    local_server.save_settings({"GITHUB_COURSE_ORG": "course-org", "STRICT_ROSTER": "true"})
    assert run_wizard(monkeypatch, ["1", "2", "", "1", "2", ""], ["admin-test-secret"]) == 0
    assert local_server.load_settings() == {
        "ADMIN_TOKEN": "admin-test-secret",
        "GITHUB_COURSE_ORG": "course-org",
        "STRICT_ROSTER": "false",
    }


def test_deferring_does_not_overwrite_saved_settings(settings_path, monkeypatch):
    assert run_wizard(monkeypatch, [""]) == 0
    assert not settings_path.exists()

    local_server.save_settings({"ADMIN_TOKEN": "admin-test-secret"})
    assert run_wizard(monkeypatch, [""]) == 0
    assert local_server.load_settings() == {"ADMIN_TOKEN": "admin-test-secret"}


def test_noninteractive_configure_skips_prompts(settings_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["local_server", "configure"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert local_server.main() == 0
    assert not settings_path.exists()


def test_start_loads_saved_settings_and_respects_environment(settings_path, monkeypatch):
    local_server.save_settings({"ADMIN_TOKEN": "stored", "STRICT_ROSTER": "true"})
    monkeypatch.setenv("ADMIN_TOKEN", "from-environment")
    monkeypatch.setenv("STRICT_ROSTER", "placeholder")
    monkeypatch.delenv("STRICT_ROSTER")
    monkeypatch.setattr(sys, "argv", ["local_server", "start", "--check-repo", "example"])
    launches = []
    monkeypatch.setattr(startup_checks, "serve", lambda repo: launches.append(repo) or 0)

    assert local_server.main() == 0
    assert local_server.os.environ["ADMIN_TOKEN"] == "from-environment"
    assert local_server.os.environ["STRICT_ROSTER"] == "true"
    assert launches == ["example"]


def test_invalid_settings_report_error_without_showing_contents(settings_path, monkeypatch, capsys):
    settings_path.write_text('{"ADMIN_TOKEN": 123456}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["local_server", "start"])
    assert local_server.main() == 1
    assert "123456" not in capsys.readouterr().err

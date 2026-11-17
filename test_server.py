import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src import database
from src.server import Member, mcp, register_team, submit_lab_results


@pytest.fixture
def grader_db(tmp_path, monkeypatch):
    path = tmp_path / "grader.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    return path


def test_register_team_never_stores_the_returned_token(grader_db):
    result = asyncio.run(mcp.call_tool("register_team", {
        "team_name": "Equipo Uno",
        "section": "02",
        "modality": "individual",
        "members": [{"github_user": "alumna-uno", "carnet": "1234524"}],
    }))
    token = result[1]["result"]
    assert token

    with closing(sqlite3.connect(grader_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM teams").fetchone()
        assert "team_token" not in row.keys()
        assert token not in repr(tuple(row))
        token_prefix, secret = token.split(".")
        assert secret not in repr(tuple(row))
        assert row["salt"] == token_prefix
        assert len(row["token_hash"]) == 64


def test_duplicate_member_rolls_back_team_and_identity_claims(grader_db):
    register_team("Primero", "01", "individual", [Member(github_user="alumna", carnet="1234524")])
    new_member = Member(github_user="otro", carnet="1234624")
    duplicate = Member(github_user="ALUMNA", carnet="1234724")
    with pytest.raises(ValueError, match="repetido"):
        register_team("Segundo", "02", "pareja", [new_member, duplicate])
    assert register_team("Segundo", "02", "individual", [new_member])


def test_submission_receipt_uses_authenticated_section_and_increments_attempt(grader_db):
    token = register_team("Equipo", "02", "individual", [Member(github_user="alumna", carnet="1234524")])
    request = SimpleNamespace(headers={"authorization": f"Bearer {token}"})
    context = SimpleNamespace(request_context=SimpleNamespace(request=request))
    first = submit_lab_results("D1", "primera", "abc1234", "{}", True, context)
    second = submit_lab_results("D1", "segunda", "abc1234", "{}", False, context)
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

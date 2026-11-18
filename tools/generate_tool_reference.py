"""Print the JSON-RPC tools/list response from an isolated Grader instance."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from src import database
from src.server import create_server

INTERFACE_VERSION = 2


def main():
    old_db = database.DB_PATH
    old_log = os.environ.get("GRADER_LOG_PATH")
    with TemporaryDirectory() as directory:
        database.DB_PATH = Path(directory) / "grader.db"
        os.environ["GRADER_LOG_PATH"] = str(Path(directory) / "grader.log")
        try:
            with TestClient(create_server().streamable_http_app(), base_url="http://localhost:8000") as client:
                response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                response.raise_for_status()
                payload = response.json()
                if "tools" not in payload.get("result", {}):
                    raise RuntimeError("tools/list no devolvió una lista de herramientas.")
                print(json.dumps({"interface_version": INTERFACE_VERSION, **payload}, ensure_ascii=False, indent=2))
        finally:
            database.DB_PATH = old_db
            if old_log is None:
                os.environ.pop("GRADER_LOG_PATH", None)
            else:
                os.environ["GRADER_LOG_PATH"] = old_log


if __name__ == "__main__":
    main()

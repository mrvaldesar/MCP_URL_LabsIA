"""Validate the local installation once Uvicorn has bound its socket."""

import asyncio
import os
import sqlite3

import httpx
from starlette.concurrency import run_in_threadpool
import uvicorn

from src import database
from src.admin import is_admin_token
from src.github import ORG_PATTERN, REPO_PATTERN
from src.server import mcp


async def check_github(repository: str | None) -> tuple[str, str]:
    organization = os.getenv("GITHUB_COURSE_ORG", "")
    token = os.getenv("GITHUB_API_TOKEN", "")
    if not organization or not token:
        return "PENDIENTE", "Configura GITHUB_COURSE_ORG y GITHUB_API_TOKEN."
    if not ORG_PATTERN.fullmatch(organization):
        return "ERROR", "GITHUB_COURSE_ORG debe ser el nombre de la organización."
    if repository is not None and (
        not REPO_PATTERN.fullmatch(repository) or repository in (".", "..")
    ):
        return "ERROR", "Indica solo el nombre del repositorio."

    if repository is None:
        path = f"/orgs/{organization}/repos"
        target = organization
    else:
        path = f"/repos/{organization}/{repository}/commits"
        target = f"{organization}/{repository}"

    try:
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=5.0,
        ) as client:
            response = await asyncio.wait_for(
                client.get(path, params={"per_page": 1}),
                timeout=6.0,
            )
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
        return "ERROR", "No se pudo completar la consulta. Revisa la conexión y la credencial."
    if response.status_code != 200:
        detail = (
            "y que el repositorio tenga al menos un commit." if repository is not None
            else "y la organización."
        )
        return "ERROR", (
            f"GitHub devolvió HTTP {response.status_code} para {target}. "
            f"Revisa el token, los permisos {detail}"
        )
    if repository is not None:
        return "OK", f"Lectura de Contents en {target}."

    try:
        repositories = response.json()
    except ValueError:
        return "ERROR", "GitHub devolvió una lista de repositorios inválida."
    if not isinstance(repositories, list):
        return "ERROR", "GitHub devolvió una lista de repositorios inválida."
    if not repositories:
        return "PENDIENTE", f"GitHub no devolvió repositorios visibles en {organization}."
    return "OK", f"Token aceptado y repositorios visibles en {organization}."


async def report_startup(repository: str | None) -> None:
    try:
        db_ok = await run_in_threadpool(database.check_health)
    except sqlite3.Error:
        db_ok = False
    db_result = ("OK", "SQLite y migraciones.") if db_ok else ("ERROR", "Revisa SQLite y las migraciones.")

    token = os.getenv("ADMIN_TOKEN", "")
    if not token:
        admin_result = ("PENDIENTE", "Configura ADMIN_TOKEN.")
    elif not db_ok:
        admin_result = ("PENDIENTE", "Primero corrige SQLite.")
    else:
        try:
            admin_ok = await is_admin_token(token)
        except sqlite3.Error:
            admin_result = ("ERROR", "No se pudo comprobar el token contra SQLite.")
        else:
            admin_result = (
                ("OK", "Credencial docente habilitada.")
                if admin_ok else ("ERROR", "Usa una credencial distinta de los tokens de equipo.")
            )

    github_result = await check_github(repository)
    results = {"SQLite": db_result, "Administración": admin_result, "GitHub": github_result}
    for name, (status, message) in results.items():
        print(f"[{status}] {name}: {message}", flush=True)
    if all(status == "OK" for status, _ in results.values()):
        print("\n========================================\n"
              "          MCP     ONLINE           \n"
              "========================================\n", flush=True)
    else:
        print("Servidor en ejecución. Revisa la configuración indicada arriba.", flush=True)


def serve(repository: str | None) -> int:
    class LocalServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                await report_startup(repository)

    config = uvicorn.Config(
        mcp.streamable_http_app(), host=mcp.settings.host, port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
    server = LocalServer(config)
    server.run()
    return 0 if server.started else 1

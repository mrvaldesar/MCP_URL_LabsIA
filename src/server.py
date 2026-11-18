import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src import database
from src.admin import ROUTES
from src.audit import AuditMiddleware, audit_log, logger
from src.auth import TeamAuthMiddleware
from src.database import Receipt, get_team_repository, init_db, register_team_db, save_submission_db
from src.github import validate_repository, verify_submission
from src.models import CARNET_PATTERN, Member, Modality, Section, validate_team_size

COMMIT_PATTERN = r"^[0-9a-f]{7,40}$"
MAX_METRICS_BYTES = 64 * 1024

DeliverableId = Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10"]


async def health(request: Request):
    try:
        migrations_match = await run_in_threadpool(database.check_health)
    except sqlite3.Error:
        logger.exception("Falló el chequeo de SQLite")
        return JSONResponse({"status": "unavailable"}, status_code=503)
    if not migrations_match:
        logger.warning("Las migraciones aplicadas no coinciden con los archivos del despliegue")
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


class GraderMCP(FastMCP):
    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        transport_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app: Starlette):
            with audit_log():
                try:
                    init_db()
                    re.compile(os.getenv("CARNET_PATTERN", CARNET_PATTERN))
                    if database.roster_is_empty():
                        logger.warning("El padrón está vacío. Carga el padrón de cada sección antes de abrir el registro.")
                except Exception:
                    logger.exception("No se pudo inicializar SQLite")
                    raise
                async with transport_lifespan(app):
                    yield

        app.router.lifespan_context = lifespan
        app.router.routes.append(Route("/health", health, methods=["GET"]))
        app.router.routes.extend(ROUTES)
        app.add_middleware(TeamAuthMiddleware, path=self.settings.streamable_http_path)
        app.add_middleware(AuditMiddleware)
        return app


async def register_team(
    team_name: str,
    section: Section,
    modality: Modality,
    members: Annotated[list[Member], Field(min_length=1, max_length=3)],
    ctx: Context,
    github_repository: str | None = None,
) -> str:
    """Registra el equipo y confirma el registro.

    El cliente HTTP recibe la credencial por separado. El agente no lee ni
    escribe archivos .env ni guarda el token en el repositorio.
    """
    request = ctx.request_context.request
    if request is None:
        raise ValueError("El registro requiere el transporte HTTP.")
    team_name = team_name.strip()
    if not team_name:
        raise ValueError("El nombre del equipo es obligatorio.")
    validate_team_size(modality, len(members))

    carnet_limit = request.state.carnet_limit
    for member in members:
        retry_after = carnet_limit.retry_after(member.carnet, consume=False)
        if retry_after is not None:
            raise ValueError(f"Demasiados intentos de registro para un carné. Reintenta en {retry_after} segundos.")

    try:
        warnings = []
        if github_repository is not None:
            warnings = await validate_repository(github_repository, section, [m.github_user for m in members])
        request.state.issued_team_token = await run_in_threadpool(
            register_team_db, team_name, section, modality, [m.model_dump() for m in members], github_repository
        )
    except database.RosterMismatch as exc:
        for carnet in set(exc.carnets):
            carnet_limit.retry_after(carnet)
        raise
    except ValueError:
        raise
    except Exception:
        logger.exception("Falló el registro del equipo")
        raise ValueError("Error interno del servidor.") from None
    return " ".join(["Equipo registrado.", *warnings])


def _reject_non_json_constant(value: str):
    raise ValueError(f"Constante JSON inválida: {value}")


async def submit_lab_results(
    deliverable_id: DeliverableId,
    label: Annotated[str, Field(description="Etiqueta de GitHub que debe coincidir exactamente con deliverable_id.")],
    commit_hash: Annotated[str, Field(pattern=COMMIT_PATTERN)],
    metrics_json: Annotated[
        str,
        Field(
            max_length=MAX_METRICS_BYTES,
            description="Métricas declaradas por el equipo en JSON. Límite estricto de "
            f"{MAX_METRICS_BYTES // 1024} KiB ({MAX_METRICS_BYTES} bytes) en UTF-8.",
        ),
    ],
    is_ci_passing: Annotated[bool, Field(description="Estado de CI declarado por el equipo.")],
    ctx: Context,
) -> Receipt:
    """Registra métricas y estado de CI declarados y retorna un recibo.

    El cliente MCP envía el token en Authorization: Bearer. El agente no lee
    ni escribe archivos .env. El recibo confirma la recepción de los datos.
    """
    request = ctx.request_context.request
    team_id = getattr(request.state, "team_id", None) if request is not None else None
    if team_id is None:
        raise ValueError("Se requiere un equipo autenticado.")

    if not re.fullmatch(re.escape(deliverable_id), label):
        raise ValueError(f"label debe coincidir exactamente con deliverable_id ({deliverable_id}).")
    if len(metrics_json.encode("utf-8")) > MAX_METRICS_BYTES:
        raise ValueError(f"metrics_json supera {MAX_METRICS_BYTES} bytes.")
    try:
        json.loads(metrics_json, parse_constant=_reject_non_json_constant)
    except (ValueError, RecursionError):
        raise ValueError("metrics_json debe ser un JSON válido.") from None

    try:
        repository = await run_in_threadpool(get_team_repository, team_id)
        if repository is None:
            raise ValueError("El equipo no tiene un repositorio de GitHub asignado. Avisa al docente.")
        verified_sha = await verify_submission(repository, label, commit_hash)
        return await run_in_threadpool(
            save_submission_db, team_id, deliverable_id, label, verified_sha, metrics_json, is_ci_passing, repository
        )
    except ValueError:
        raise
    except Exception:
        logger.exception("Falló el almacenamiento del envío")
        raise ValueError("Error interno del servidor.") from None


def create_server() -> GraderMCP:
    server = GraderMCP(
        "Landivar_Grader_MCP", host="0.0.0.0", port=8000, json_response=True, stateless_http=True
    )
    annotations = ToolAnnotations(readOnlyHint=False, idempotentHint=False, destructiveHint=False)
    server.add_tool(register_team, annotations=annotations)
    server.add_tool(submit_lab_results, annotations=annotations)
    return server


mcp = create_server()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=8000, proxy_headers=False)

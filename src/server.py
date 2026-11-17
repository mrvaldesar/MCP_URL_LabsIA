import json
import re
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette

from src.auth import TeamAuthMiddleware
from src.database import Receipt, init_db, register_team_db, save_submission_db

CARNET_PATTERN = r"^[0-9]{7}$"
GITHUB_USER_PATTERN = r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$"
DELIVERABLE_PATTERN = r"^D([1-9]|10)$"
COMMIT_PATTERN = r"^[0-9a-f]{7,40}$"
MAX_METRICS_BYTES = 64 * 1024
TEAM_SIZES = {"individual": 1, "pareja": 2, "trio": 3}

DeliverableId = Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10"]


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_user: Annotated[str, Field(max_length=39, pattern=GITHUB_USER_PATTERN)]
    carnet: Annotated[str, Field(pattern=CARNET_PATTERN)]


class GraderMCP(FastMCP):
    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        transport_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app: Starlette):
            init_db()
            async with transport_lifespan(app):
                yield

        app.router.lifespan_context = lifespan
        app.add_middleware(TeamAuthMiddleware, path=self.settings.streamable_http_path)
        return app


def register_team(
    team_name: str,
    section: Literal["01", "02"],
    modality: Literal["individual", "pareja", "trio"],
    members: Annotated[list[Member], Field(min_length=1, max_length=3)],
    ctx: Context,
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
    if len(members) != TEAM_SIZES[modality]:
        raise ValueError(f"La modalidad {modality} requiere {TEAM_SIZES[modality]} integrante(s).")

    request.state.issued_team_token = register_team_db(
        team_name, section, modality, [member.model_dump() for member in members]
    )
    return "Equipo registrado."


def _reject_non_json_constant(value: str):
    raise ValueError(f"Constante JSON inválida: {value}")


def submit_lab_results(
    deliverable_id: DeliverableId,
    label: str,
    commit_hash: Annotated[str, Field(pattern=COMMIT_PATTERN)],
    metrics_json: Annotated[str, Field(max_length=MAX_METRICS_BYTES, description="Métricas declaradas por el equipo.")],
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

    if not re.fullmatch(DELIVERABLE_PATTERN, deliverable_id):
        raise ValueError("deliverable_id debe estar entre D1 y D10.")
    if not re.fullmatch(COMMIT_PATTERN, commit_hash):
        raise ValueError("commit_hash debe contener entre 7 y 40 dígitos hexadecimales en minúscula.")
    label = label.strip()
    if not label:
        raise ValueError("label es obligatorio.")
    if len(metrics_json.encode("utf-8")) > MAX_METRICS_BYTES:
        raise ValueError(f"metrics_json supera {MAX_METRICS_BYTES} bytes.")
    try:
        json.loads(metrics_json, parse_constant=_reject_non_json_constant)
    except (ValueError, RecursionError):
        raise ValueError("metrics_json debe ser un JSON válido.") from None

    return save_submission_db(team_id, deliverable_id, label, commit_hash, metrics_json, is_ci_passing)


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
    mcp.run(transport="streamable-http")

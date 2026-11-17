import json
import re
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette

from src.database import Receipt, init_db, register_team_db, save_submission_db

CARNET_PATTERN = r"^[0-9]{7}$"
GITHUB_USER_PATTERN = r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$"
DELIVERABLE_PATTERN = r"^D([1-9]|10)$"
COMMIT_PATTERN = r"^[0-9a-f]{7,40}$"
TOKEN_PATTERN = r"(?:[0-9a-f]{32}\.[0-9a-f]{64}|[0-9a-f]{32})"
MAX_METRICS_BYTES = 64 * 1024
TEAM_SIZES = {"individual": 1, "pareja": 2, "trio": 3}

DeliverableId = Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10"]


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_user: Annotated[str, Field(max_length=39, pattern=GITHUB_USER_PATTERN)]
    carnet: Annotated[str, Field(pattern=CARNET_PATTERN)]


@asynccontextmanager
async def lifespan(app: Starlette):
    init_db()
    yield


mcp = FastMCP("Landivar_Grader_MCP", host="0.0.0.0", port=8000)


@mcp.tool()
def register_team(
    team_name: str,
    section: Literal["01", "02"],
    modality: Literal["individual", "pareja", "trio"],
    members: Annotated[list[Member], Field(min_length=1, max_length=3)],
) -> str:
    """Registra el equipo y retorna su token una sola vez.

    Entrega el token al estudiante para que configure el cliente MCP. El agente
    no lee ni escribe archivos .env ni guarda el token en el repositorio.
    """
    team_name = team_name.strip()
    if not team_name:
        raise ValueError("El nombre del equipo es obligatorio.")
    if len(members) != TEAM_SIZES[modality]:
        raise ValueError(f"La modalidad {modality} requiere {TEAM_SIZES[modality]} integrante(s).")

    return register_team_db(team_name, section, modality, [member.model_dump() for member in members])


def _reject_non_json_constant(value: str):
    raise ValueError(f"Constante JSON inválida: {value}")


@mcp.tool()
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
    authorization = request.headers.get("authorization", "") if request is not None else ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not re.fullmatch(TOKEN_PATTERN, token):
        raise ValueError("Se requiere un token de equipo válido en Authorization: Bearer.")

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

    return save_submission_db(token, deliverable_id, label, commit_hash, metrics_json, is_ci_passing)


app = mcp.sse_app()
app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)

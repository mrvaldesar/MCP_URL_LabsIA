import hmac
import io
import os
import sqlite3
from functools import wraps

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from src import database
from src.audit import logger
from src.auth import MAX_REQUEST_BYTES, TOKEN_PATTERN
from src.export import export_section
from src.models import Member, Section

NO_STORE = {"Cache-Control": "no-store"}


def admin_only(endpoint):
    @wraps(endpoint)
    async def protected(request: Request):
        expected = os.getenv("ADMIN_TOKEN")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        valid = (
            bool(expected)
            and scheme.lower() == "bearer"
            and hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
        )
        if valid and TOKEN_PATTERN.fullmatch(token):
            valid = await run_in_threadpool(database.resolve_team_id, token) is None
        if not valid:
            return JSONResponse({"error": "Acceso administrativo denegado."}, status_code=403)
        return await endpoint(request)
    return protected


@admin_only
async def rotate_token(request: Request):
    team_id = request.path_params["id"]
    if not 0 < team_id <= 2**63 - 1:
        return JSONResponse({"error": "Equipo no encontrado."}, status_code=404)
    token = await run_in_threadpool(database.rotate_team_token, team_id)
    if token is None:
        return JSONResponse({"error": "Equipo no encontrado."}, status_code=404)
    logger.info("Credencial rotada para equipo id=%d", team_id)
    return JSONResponse({"team_id": team_id}, headers={**NO_STORE, "X-Team-Token": token})


@admin_only
async def download_export(request: Request):
    section = request.query_params.get("section")
    if section not in ("01", "02"):
        return JSONResponse({"error": "La sección debe ser 01 o 02."}, status_code=400)
    output = io.StringIO(newline="")
    await run_in_threadpool(export_section, section, output)
    return Response(output.getvalue(), media_type="text/csv", headers={
        **NO_STORE, "Content-Disposition": f'attachment; filename="section-{section}.csv"',
    })


class Roster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: Section
    members: list[Member] = Field(max_length=10_000)


@admin_only
async def upload_roster(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            return JSONResponse({"error": "La solicitud supera 1 MiB."}, status_code=413)
        body.extend(chunk)
    try:
        roster = Roster.model_validate_json(body)
    except ValidationError:
        return JSONResponse({"error": "Padrón inválido. Revisa la sección y los integrantes."}, status_code=400)
    members = [(member.github_user, member.carnet) for member in roster.members]
    try:
        await run_in_threadpool(database.replace_roster, roster.section, members)
    except sqlite3.IntegrityError:
        return JSONResponse({"error": "El padrón contiene usuarios GitHub o carnés repetidos."}, status_code=409)
    logger.info("Padrón actualizado sección=%s integrantes=%d", roster.section, len(members))
    return JSONResponse({"section": roster.section, "members_imported": len(members)})


ROUTES = [
    Route("/admin/teams/{id:int}/rotate-token", rotate_token, methods=["POST"]),
    Route("/admin/export", download_export, methods=["GET"]),
    Route("/admin/roster", upload_roster, methods=["POST"]),
]

import hashlib
import hmac
import json
import io
import os
import sqlite3
from datetime import timezone
from functools import wraps

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from src import database, github, verification
from src.audit import logger
from src.auth import MAX_REQUEST_BYTES, TOKEN_PATTERN
from src.export import export_section
from src.models import DeliverableId, Member, Modality, Section

NO_STORE = {"Cache-Control": "no-store"}


class TeacherCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    section: Section | None = None
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def teacher_credentials() -> list[TeacherCredential]:
    entries = json.loads(os.getenv("TEACHER_ADMIN_CREDENTIALS", "[]"))
    if not isinstance(entries, list):
        raise ValueError("Se esperaba una lista de credenciales.")
    credentials = [TeacherCredential.model_validate(entry) for entry in entries]
    if (len({entry.id for entry in credentials}) != len(credentials)
            or len({entry.sha256 for entry in credentials}) != len(credentials)
            or any(entry.id == "superadmin" for entry in credentials)):
        raise ValueError("Los docentes deben tener identificadores y credenciales únicos.")
    return credentials


def admin_only(endpoint):
    @wraps(endpoint)
    async def protected(request: Request):
        expected = os.getenv("ADMIN_TOKEN")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        identity = None
        section = None
        if scheme.lower() == "bearer" and token:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if expected and hmac.compare_digest(digest, hashlib.sha256(expected.encode("utf-8")).hexdigest()):
                identity = "superadmin"
            else:
                try:
                    for teacher in teacher_credentials():
                        if hmac.compare_digest(digest, teacher.sha256):
                            identity, section = teacher.id, teacher.section
                except ValueError:
                    logger.error("TEACHER_ADMIN_CREDENTIALS tiene formato inválido.")
        if identity and TOKEN_PATTERN.fullmatch(token) and await run_in_threadpool(database.resolve_team_id, token):
            identity = None
        if identity is None:
            return JSONResponse({"error": "Acceso administrativo denegado."}, status_code=403)
        request.state.admin_section = section
        request.state.admin_actor = identity
        response = await endpoint(request)
        resource = getattr(request.state, "audit_resource", request.path_params.get("id", "-"))
        logger.info("Administración actor=%s acción=%s recurso=%s estado=%d",
                    identity, endpoint.__name__, resource, response.status_code)
        return response
    return protected


def section_denied(request: Request, section: str) -> bool:
    return request.state.admin_section not in (None, section)


def forbidden():
    return JSONResponse({"error": "Tu credencial administrativa no tiene acceso a esa sección."}, status_code=403)


@admin_only
async def rotate_token(request: Request):
    team_id = request.path_params["id"]
    if not 0 < team_id <= 2**63 - 1:
        return JSONResponse({"error": "Equipo no encontrado."}, status_code=404)
    token = await run_in_threadpool(database.rotate_team_token, team_id, request.state.admin_section)
    if token is None:
        return JSONResponse({"error": "Equipo no encontrado."}, status_code=404)
    logger.info("Credencial rotada para equipo id=%d", team_id)
    return JSONResponse({"team_id": team_id}, headers={**NO_STORE, "X-Team-Token": token})


@admin_only
async def download_export(request: Request):
    section = request.query_params.get("section")
    if section not in ("01", "02"):
        return JSONResponse({"error": "La sección debe ser 01 o 02."}, status_code=400)
    request.state.audit_resource = section
    if section_denied(request, section):
        return forbidden()
    output = io.StringIO(newline="")
    await run_in_threadpool(export_section, section, output)
    return Response(output.getvalue(), media_type="text/csv", headers={
        **NO_STORE, "Content-Disposition": f'attachment; filename="section-{section}.csv"',
    })


class Roster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: Section
    members: list[Member] = Field(max_length=10_000)


async def read_admin_body(request: Request) -> bytearray | None:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            return None
        body.extend(chunk)
    return body


class TeamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    members: list[Member] | None = Field(default=None, min_length=1, max_length=3)
    modality: Modality | None = None


@admin_only
async def update_team(request: Request):
    team_id = request.path_params["id"]
    if not 0 < team_id <= 2**63 - 1:
        return JSONResponse({"error": "Equipo no encontrado."}, status_code=404)
    body = await read_admin_body(request)
    if body is None:
        return JSONResponse({"error": "La solicitud supera 1 MiB."}, status_code=413)
    try:
        update = TeamUpdate.model_validate_json(body)
    except ValidationError:
        return JSONResponse({"error": "Actualización inválida. Revisa la modalidad y los integrantes."}, status_code=400)
    if update.members is None and update.modality is None:
        return JSONResponse({"error": "Indica members o modality."}, status_code=400)

    members = [member.model_dump() for member in update.members] if update.members is not None else None
    try:
        token = await run_in_threadpool(database.update_team, team_id, members, update.modality, request.state.admin_section)
    except database.MemberIdentityConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if token is None:
        return JSONResponse({"error": "Equipo no encontrado."}, status_code=404)
    logger.info("Equipo actualizado y credencial rotada id=%d", team_id)
    return JSONResponse({"team_id": team_id}, headers={**NO_STORE, "X-Team-Token": token})


@admin_only
async def upload_roster(request: Request):
    body = await read_admin_body(request)
    if body is None:
        return JSONResponse({"error": "La solicitud supera 1 MiB."}, status_code=413)
    try:
        roster = Roster.model_validate_json(body)
    except ValidationError:
        return JSONResponse({"error": "Padrón inválido. Revisa la sección y los integrantes."}, status_code=400)
    request.state.audit_resource = roster.section
    if section_denied(request, roster.section):
        return forbidden()
    members = [(member.github_user, member.carnet) for member in roster.members]
    try:
        await run_in_threadpool(database.replace_roster, roster.section, members)
    except sqlite3.IntegrityError:
        return JSONResponse({"error": "El padrón contiene usuarios GitHub o carnés repetidos."}, status_code=409)
    logger.info("Padrón actualizado sección=%s integrantes=%d", roster.section, len(members))
    return JSONResponse({"section": roster.section, "members_imported": len(members)})


@admin_only
async def github_health(request: Request):
    status = await github.credential_status()
    coverage = await github.app_repository_coverage()
    if coverage == "seleccionados":
        logger.warning("La GitHub App debe instalarse para todos los repositorios de la organización")
    return JSONResponse({"github_credential_status": status,
                         "github_app_repository_coverage": coverage}, headers=NO_STORE)


@admin_only
async def retry_verification(request: Request):
    receipt_id = request.path_params["id"]
    if not 0 < receipt_id <= 2**63 - 1:
        return JSONResponse({"error": "Recibo pendiente no encontrado."}, status_code=404)
    outcome, payload = await verification.retry(receipt_id, request.state.admin_section, request.state.admin_actor)
    if outcome == "ausente":
        return JSONResponse({"error": "Recibo pendiente no encontrado."}, status_code=404)
    if outcome == "agotado":
        return JSONResponse({"error": "Se alcanzó el máximo de reintentos del recibo."}, status_code=409)
    if outcome == "programado":
        count, next_at = payload
        return JSONResponse({"receipt_id": receipt_id, "verification_status": "pendiente",
                             "verification_retry_count": count, "next_retry_at_epoch": next_at or None},
                            status_code=202)
    if outcome in ("en_curso", "cambió"):
        return JSONResponse({"error": "El recibo cambió o ya se está verificando. Consulta el CSV."}, status_code=409)
    return JSONResponse(payload, status_code=202 if outcome == "pendiente" else 200)


@admin_only
async def reset_verification(request: Request):
    receipt_id = request.path_params["id"]
    if not 0 < receipt_id <= 2**63 - 1:
        return JSONResponse({"error": "Recibo pendiente no encontrado."}, status_code=404)
    outcome = await run_in_threadpool(database.reset_verification, receipt_id, request.state.admin_section)
    if outcome == "ausente":
        return JSONResponse({"error": "Recibo pendiente no encontrado."}, status_code=404)
    if outcome == "en_curso":
        return JSONResponse({"error": "El recibo ya se está verificando."}, status_code=409)
    return JSONResponse({"receipt_id": receipt_id, "verification_retry_count": 0})


class DeliveryWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: Section
    deliverable_id: DeliverableId
    opens_at: AwareDatetime
    closes_at: AwareDatetime


@admin_only
async def set_window(request: Request):
    body = await read_admin_body(request)
    if body is None:
        return JSONResponse({"error": "La solicitud supera 1 MiB."}, status_code=413)
    try:
        window = DeliveryWindow.model_validate_json(body)
    except ValidationError:
        return JSONResponse({"error": "Indica sección, entregable y fechas ISO 8601 con zona horaria."}, status_code=400)
    request.state.audit_resource = f"{window.section}/{window.deliverable_id}"
    if section_denied(request, window.section):
        return forbidden()
    if window.opens_at >= window.closes_at:
        return JSONResponse({"error": "El cierre debe ser posterior a la apertura."}, status_code=400)
    opens, closes = (value.astimezone(timezone.utc).isoformat(timespec="microseconds")
                    for value in (window.opens_at, window.closes_at))
    await run_in_threadpool(database.set_delivery_window, window.section, window.deliverable_id, opens, closes)
    logger.info("Ventana configurada sección=%s entregable=%s", window.section, window.deliverable_id)
    return JSONResponse({"section": window.section, "deliverable_id": window.deliverable_id,
                         "opens_at_utc": opens, "closes_at_utc": closes})


@admin_only
async def list_teams(request: Request):
    section = request.query_params.get("section", request.state.admin_section)
    if section not in (None, "01", "02"):
        return JSONResponse({"error": "La sección debe ser 01 o 02."}, status_code=400)
    request.state.audit_resource = section or "todas"
    if section is not None and section_denied(request, section):
        return forbidden()
    teams = await run_in_threadpool(database.list_teams, section)
    return JSONResponse({"teams": teams}, headers=NO_STORE)


class AssignmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: Section | None = None
    github_repository: str | None = None


@admin_only
async def change_assignment(request: Request):
    team_id = request.path_params["id"]
    assignment = None
    if 0 < team_id <= 2**63 - 1:
        assignment = await run_in_threadpool(database.team_assignment, team_id, request.state.admin_section)
    if assignment is None:
        return JSONResponse({"error": "Equipo no encontrado."}, status_code=404)
    body = await read_admin_body(request)
    if body is None:
        return JSONResponse({"error": "La solicitud supera 1 MiB."}, status_code=413)
    try:
        update = AssignmentUpdate.model_validate_json(body)
    except ValidationError:
        return JSONResponse({"error": "Indica section (01 o 02) o github_repository (nombre corto)."}, status_code=400)
    if update.section is None and update.github_repository is None:
        return JSONResponse({"error": "Indica la sección de destino o el repositorio corregido."}, status_code=400)
    section = update.section or assignment[0]
    if section_denied(request, section):
        return forbidden()
    repository = update.github_repository if update.github_repository is not None else assignment[1]
    try:
        if repository is not None and (update.github_repository is not None or section != assignment[0]):
            await github.validate_repository(repository, section)
        changed = await run_in_threadpool(database.change_assignment, team_id, assignment, section, repository)
    except github.GitHubUnavailable as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not changed:
        return JSONResponse({"error": "El equipo cambió. Actualiza la lista y reintenta."}, status_code=409)
    return JSONResponse({"team_id": team_id, "section": section, "github_repository": repository})


ROUTES = [
    Route("/admin/teams", list_teams, methods=["GET"]),
    Route("/admin/teams/{id:int}/assignment", change_assignment, methods=["PATCH"]),
    Route("/admin/windows", set_window, methods=["PUT"]),
    Route("/admin/github/health", github_health, methods=["GET"]),
    Route("/admin/submissions/{id:int}/reset-verification", reset_verification, methods=["POST"]),
    Route("/admin/submissions/{id:int}/retry-verification", retry_verification, methods=["POST"]),
    Route("/admin/teams/{id:int}/update", update_team, methods=["POST"]),
    Route("/admin/teams/{id:int}/rotate-token", rotate_token, methods=["POST"]),
    Route("/admin/export", download_export, methods=["GET"]),
    Route("/admin/roster", upload_roster, methods=["POST"]),
]

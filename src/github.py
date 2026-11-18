import os
import re
from urllib.parse import quote

import httpx

SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
ORG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")
COURSE_REPO_PATTERN = re.compile(r"lab-ia-s(01|02)-[a-z0-9-]{3,40}")


class GitHubVerificationError(ValueError):
    pass


class GitHubUnavailable(GitHubVerificationError):
    pass


class GitHubNotFound(GitHubVerificationError):
    pass


def _client(*, public=False):
    token = "" if public else os.getenv("GITHUB_API_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(base_url="https://api.github.com", headers=headers, timeout=5.0)


def _repo_path(repository: str) -> str:
    organization = os.getenv("GITHUB_COURSE_ORG", "")
    if not os.getenv("GITHUB_API_TOKEN") or not ORG_PATTERN.fullmatch(organization):
        raise GitHubUnavailable("La verificación de GitHub requiere configuración del servidor. Avisa al docente.")
    if not REPO_PATTERN.fullmatch(repository) or repository in (".", ".."):
        raise GitHubVerificationError("El repositorio asignado al equipo es inválido. Avisa al docente.")
    return f"/repos/{organization}/{repository}"


async def _request(client: httpx.AsyncClient, path: str) -> httpx.Response:
    try:
        return await client.get(path)
    except httpx.RequestError:
        raise GitHubUnavailable("GitHub no está disponible. Intenta más tarde.") from None


async def _get(client: httpx.AsyncClient, path: str) -> dict:
    response = await _request(client, path)
    if response.status_code == 404:
        raise GitHubNotFound()
    if response.is_success:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            return payload
    raise GitHubUnavailable("GitHub no pudo verificar la solicitud. Intenta más tarde o avisa al docente.")


async def validate_repository(repository: str, section: str, users=()) -> list[str]:
    match = COURSE_REPO_PATTERN.fullmatch(repository)
    if match is None:
        raise ValueError("Usa el nombre corto lab-ia-s01-<equipo> o lab-ia-s02-<equipo> con un sufijo de 3 a 40 caracteres.")
    if match[1] != section:
        raise ValueError("El prefijo del repositorio debe coincidir con la sección del equipo.")
    path = _repo_path(repository)
    async with _client() as client:
        try:
            metadata = await _get(client, path)
        except GitHubNotFound:
            raise ValueError("El repositorio no existe o la credencial del curso no puede verlo. Verifica el nombre o consulta al docente.") from None
        full_name = metadata.get("full_name")
        if not isinstance(full_name, str) or full_name.lower() != path.removeprefix("/repos/").lower():
            raise GitHubUnavailable("GitHub devolvió datos inesperados. Reintenta el registro.")
        if metadata.get("private") is not True:
            raise ValueError("El repositorio del equipo debe ser privado.")
        warnings = []
        for user in users:
            try:
                response = await _request(client, f"{path}/collaborators/{quote(user, safe='')}")
            except GitHubUnavailable:
                break
            if response.status_code == 404:
                # GitHub can mask permission errors as 404. Confirm access to
                # the collaborators API before interpreting it as non-membership.
                try:
                    probe = await _request(client, f"{path}/collaborators?per_page=1")
                except GitHubUnavailable:
                    break
                if probe.status_code == 200:
                    warnings.append(f"{user} no figura como colaborador del repositorio.")
        return warnings


def _sha(data: dict) -> str:
    sha = data.get("sha")
    if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
        raise GitHubUnavailable("GitHub devolvió un SHA inválido. Intenta más tarde.")
    return sha


async def _check_visibility(client, path):
    try:
        repo = await _get(client, path)
    except GitHubNotFound:
        raise GitHubUnavailable("GitHub no permite consultar el repositorio. Avisa al docente.") from None
    if not isinstance(repo.get("full_name"), str):
        raise GitHubUnavailable("GitHub devolvió datos inesperados. Intenta más tarde.")


async def resolve_tag(client, path: str, label: str, commit_hash: str) -> str:
    try:
        ref = await _get(client, f"{path}/git/ref/tags/{quote(label, safe='')}")
    except GitHubNotFound:
        await _check_visibility(client, path)
        raise GitHubVerificationError("Etiqueta no encontrada. Publica la etiqueta en el repositorio asignado.") from None
    if ref.get("ref") != f"refs/tags/{label}":
        raise GitHubUnavailable("GitHub devolvió una referencia inesperada. Intenta más tarde.")
    target = ref.get("object")
    visited = set()
    for _ in range(32):
        if not isinstance(target, dict):
            break
        sha = _sha(target)
        kind = target.get("type")
        if kind == "commit":
            try:
                commit = await _get(client, f"{path}/commits/{commit_hash}")
            except GitHubNotFound:
                await _check_visibility(client, path)
                raise GitHubVerificationError("Commit inexistente. Publica el commit y comprueba su SHA.") from None
            if _sha(commit) != sha:
                raise GitHubVerificationError("La etiqueta de GitHub apunta a otro commit. Usa el SHA al que apunta la etiqueta.")
            return sha
        if kind in ("tree", "blob"):
            raise GitHubVerificationError("La etiqueta apunta a un objeto que no es un commit. Publica una etiqueta sobre el commit.")
        if kind != "tag" or sha in visited:
            break
        visited.add(sha)
        try:
            target = (await _get(client, f"{path}/git/tags/{sha}")).get("object")
        except GitHubNotFound:
            break
    raise GitHubUnavailable("GitHub no pudo resolver la cadena de etiquetas. Avisa al docente.")


async def verify_submission(repository: str, label: str, commit_hash: str) -> str:
    path = _repo_path(repository)
    async with _client() as client:
        return await resolve_tag(client, path, label, commit_hash)


async def verified_ci_status(repository: str, sha: str) -> str:
    try:
        path = _repo_path(repository)
        async with _client() as client:
            summary = await _get(client, f"{path}/commits/{sha}/status")
            checks = await _get(client, f"{path}/commits/{sha}/check-runs?per_page=100")
        runs = checks.get("check_runs")
        count = checks.get("total_count")
        status = summary.get("state")
        statuses = summary.get("total_count")
        if not isinstance(runs, list) or count != len(runs) or not isinstance(statuses, int):
            return "desconocido"
        conclusions = set()
        for run in runs:
            if not isinstance(run, dict) or run.get("head_sha") != sha:
                return "desconocido"
            conclusion = run.get("conclusion") if run.get("status") == "completed" else "pending"
            if not isinstance(conclusion, str):
                return "desconocido"
            conclusions.add(conclusion)
        if conclusions & {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"} or (
            statuses and status in ("failure", "error")
        ):
            return "fallido"
        if "pending" in conclusions or (statuses and status == "pending"):
            return "pendiente"
        if conclusions <= {"success", "neutral", "skipped"} and (
            (not statuses and "success" in conclusions) or status == "success"
        ):
            return "exitoso"
    except GitHubVerificationError:
        pass
    return "desconocido"


async def credential_status() -> str:
    try:
        path = _repo_path("health-placeholder")
    except GitHubUnavailable:
        return "sin_configurar"
    organization = path.split("/")[2]
    try:
        async with _client() as client:
            response = await _request(client, f"/orgs/{organization}/repos?type=private&per_page=1")
    except GitHubUnavailable:
        return "indisponible"
    if response.status_code == 401:
        return "invalida"
    if not response.is_success:
        return "indisponible"
    try:
        repositories = response.json()
    except ValueError:
        return "indisponible"
    if isinstance(repositories, list) and any(
        isinstance(repo, dict) and repo.get("private") is True for repo in repositories
    ):
        return "valida"
    return "acceso_privado_no_confirmado"

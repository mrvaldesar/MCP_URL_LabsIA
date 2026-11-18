import asyncio
import os
import re
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
from time import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import jwt

from src.audit import logger

SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
ORG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")
COURSE_REPO_PATTERN = re.compile(r"lab-ia-s(01|02)-[a-z0-9-]{3,40}")


class GitHubVerificationError(ValueError):
    pass


class GitHubUnavailable(GitHubVerificationError):
    def __init__(self, message="", *, retry_at=0):
        super().__init__(message)
        self.retry_at = retry_at


class GitHubNotFound(GitHubVerificationError):
    pass


class GitHubCredentialError(GitHubUnavailable):
    pass


APP_ENV = ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH")
_token_lock = asyncio.Lock()
_installation_token = None
_credential_alert = None


def _app_config():
    return tuple(os.getenv(key, "") for key in APP_ENV)


def _credential_error(status, path=None):
    global _credential_alert
    _credential_alert = (path, status)
    logger.warning("Credencial de GitHub inválida o con permisos insuficientes estado=%s", status)
    raise GitHubCredentialError("La credencial de GitHub requiere revisión del docente.")


def _check_credential(response: httpx.Response):
    if response.status_code == 401:
        _credential_error(401, response.request.url.raw_path.decode())
    if response.status_code == 403:
        if response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers:
            return
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        message = str(payload.get("message", "")).lower() if isinstance(payload, dict) else ""
        if "rate limit" not in message and "abuse detection" not in message:
            _credential_error(403, response.request.url.raw_path.decode())


def retry_at(response: httpx.Response) -> int:
    times = [0]
    retry = response.headers.get("retry-after", "")
    try:
        times.append(int(time()) + int(retry))
    except ValueError:
        try:
            times.append(int(parsedate_to_datetime(retry).timestamp()))
        except (ValueError, OverflowError, TypeError):
            pass
    # GitHub sends the reset for successful quota usage too. Use it only
    # when throttled or when the response asks for a retry.
    if response.status_code in (403, 429) or retry:
        reset = response.headers.get("x-ratelimit-reset", "")
        if reset.isascii() and reset.isdecimal() and len(reset) <= 12:
            times.append(int(reset))
    return min(max(times), 253402300799)


def credential_alert_active() -> bool:
    return _credential_alert is not None


async def recover_credential():
    global _credential_alert
    alert = _credential_alert
    if alert is None:
        return
    path, _ = alert
    try:
        if path is None or path.startswith("/app/installations/"):
            healthy = await credential_status() == "valida"
        elif path.startswith("/orgs/") and path.endswith("/installation"):
            healthy = await app_repository_coverage() in ("todos", "seleccionados")
        else:
            async with _client() as client:
                response = await _request(client, path)
                _check_credential(response)
                healthy = response.is_success
    except GitHubUnavailable:
        return
    if healthy and _credential_alert is alert:
        _credential_alert = None
        logger.info("Se recuperó la credencial de GitHub")


def _app_assertion(config):
    app_id, installation_id, key_path = config
    if not re.fullmatch(r"[0-9]+", app_id) or not re.fullmatch(r"[0-9]+", installation_id) or not key_path:
        _credential_error("configuración_app")
    now = int(time())
    try:
        return jwt.encode({"iss": app_id, "iat": now - 60, "exp": now + 540},
                          Path(key_path).read_bytes(), algorithm="RS256")
    except (OSError, ValueError, jwt.PyJWTError):
        _credential_error("clave_app")


async def app_repository_coverage() -> str:
    config = _app_config()
    if not any(config):
        return "no_aplica"
    organization = os.getenv("GITHUB_COURSE_ORG", "")
    if not ORG_PATTERN.fullmatch(organization):
        return "desconocida"
    try:
        assertion = _app_assertion(config)
        async with _client(public=True) as client:
            client.headers["Authorization"] = f"Bearer {assertion}"
            installation = await _get(client, f"/orgs/{organization}/installation")
    except GitHubVerificationError:
        return "desconocida"
    if str(installation.get("id")) != config[1] or installation.get("suspended_at"):
        return "desconocida"
    return {"all": "todos", "selected": "seleccionados"}.get(
        installation.get("repository_selection") if isinstance(installation.get("repository_selection"), str) else "",
        "desconocida",
    )


async def _app_token(client, config):
    global _installation_token
    _, installation_id, _ = config
    async with _token_lock:
        now = datetime.now(timezone.utc).timestamp()
        if _installation_token:
            cached_config, token, expiry = _installation_token
            if cached_config == config and expiry > now + 60:
                return token
        assertion = _app_assertion(config)
        try:
            response = await client.post(f"/app/installations/{installation_id}/access_tokens",
                                         headers={"Authorization": f"Bearer {assertion}"})
        except httpx.RequestError:
            raise GitHubUnavailable("GitHub no está disponible. Intenta más tarde.") from None
        _check_credential(response)
        if response.status_code == 404:
            _credential_error("instalación_app")
        if response.status_code != 201:
            raise GitHubUnavailable("GitHub no pudo emitir el token de instalación. Intenta más tarde.", retry_at=retry_at(response))
        try:
            payload = response.json()
            token = payload["token"]
            expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
            if not isinstance(token, str) or not token or expires.tzinfo is None or expires.timestamp() <= now:
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise GitHubUnavailable("GitHub devolvió un token de instalación inválido. Avisa al docente.") from None
        _installation_token = (config, token, expires.timestamp())
        return token


@asynccontextmanager
async def _client(*, public=False):
    global _installation_token
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(base_url="https://api.github.com", headers=headers, timeout=5.0) as client:
        config = _app_config()
        token = ""
        if not public:
            token = await _app_token(client, config) if any(config) else os.getenv("GITHUB_API_TOKEN", "")
        if token:
            client.headers["Authorization"] = f"Bearer {token}"
        try:
            yield client
        except GitHubCredentialError:
            if _installation_token and _installation_token[1] == token:
                _installation_token = None
            raise


def _repo_path(repository: str) -> str:
    organization = os.getenv("GITHUB_COURSE_ORG", "")
    if not (os.getenv("GITHUB_API_TOKEN") or any(_app_config())) or not ORG_PATTERN.fullmatch(organization):
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
    _check_credential(response)
    if response.status_code == 404:
        raise GitHubNotFound()
    if response.is_success:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            return payload
    raise GitHubUnavailable("GitHub no pudo verificar la solicitud. Intenta más tarde o avisa al docente.", retry_at=retry_at(response))


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
        if not isinstance(metadata.get("private"), bool):
            raise GitHubUnavailable("GitHub devolvió datos inesperados. Reintenta el registro.")
        if not metadata["private"]:
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


async def _check_contents_access(client, path):
    # Metadata remains visible to tokens that lack Contents permission.
    response = await _request(client, f"{path}/git/matching-refs/tags")
    _check_credential(response)
    if not response.is_success:
        raise GitHubUnavailable("GitHub no permite confirmar el acceso al contenido. Avisa al docente.", retry_at=retry_at(response))
    try:
        refs = response.json()
    except ValueError:
        refs = None
    if not isinstance(refs, list):
        raise GitHubUnavailable("GitHub devolvió datos inesperados. Intenta más tarde.")


async def resolve_tag(client, path: str, label: str, commit_hash: str) -> str:
    try:
        ref = await _get(client, f"{path}/git/ref/tags/{quote(label, safe='')}")
    except GitHubNotFound:
        await _check_contents_access(client, path)
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
                await _check_contents_access(client, path)
                raise GitHubVerificationError("Commit inexistente. Publica el commit y comprueba su SHA.") from None
            if _sha(commit) != sha or not sha.startswith(commit_hash):
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
            (not statuses and "success" in conclusions) or (statuses and status == "success")
        ):
            return "exitoso"
    except GitHubCredentialError:
        raise
    except GitHubVerificationError:
        pass
    return "desconocido"


async def credential_status() -> str:
    try:
        path = _repo_path("health-placeholder")
    except GitHubUnavailable:
        return "sin_configurar"
    organization = path.split("/")[2]
    app = any(_app_config())
    try:
        async with _client() as client:
            endpoint = "/installation/repositories?per_page=100" if app else f"/orgs/{organization}/repos?type=private&per_page=1"
            response = await _request(client, endpoint)
            _check_credential(response)
    except GitHubCredentialError:
        return "invalida"
    except GitHubUnavailable:
        return "indisponible"
    if not response.is_success:
        return "indisponible"
    try:
        repositories = response.json()
        if app:
            repositories = [repo for repo in repositories["repositories"]
                            if isinstance(repo, dict) and
                            str(repo.get("full_name", "")).split("/")[0].lower() == organization.lower()]
    except (ValueError, KeyError, TypeError):
        return "indisponible"
    if isinstance(repositories, list) and any(
        isinstance(repo, dict) and repo.get("private") is True for repo in repositories
    ):
        return "valida"
    return "acceso_privado_no_confirmado"

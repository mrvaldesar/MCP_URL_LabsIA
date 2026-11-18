import os
import re
from urllib.parse import quote

import httpx

from src.audit import logger

SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
ORG_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class GitHubVerificationError(ValueError):
    pass


async def _get(client: httpx.AsyncClient, path: str) -> dict:
    try:
        response = await client.get(path)
    except httpx.RequestError:
        raise GitHubVerificationError("GitHub no está disponible. Intenta más tarde.") from None

    if response.status_code == 401:
        logger.error("GitHub rechazó la credencial del servidor (401)")
        raise GitHubVerificationError("Falló la autenticación del servidor con GitHub. Avisa al docente.")
    if response.status_code == 404:
        raise GitHubVerificationError(
            "La etiqueta o el commit no existe en el repositorio asignado o GitHub no permite consultarlo."
        )
    if response.status_code in (403, 429):
        raise GitHubVerificationError("GitHub alcanzó el límite de solicitudes o denegó el acceso. Intenta más tarde.")
    if response.is_success:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            return payload
    raise GitHubVerificationError("GitHub no pudo verificar la entrega. Intenta más tarde.")


async def verify_submission(repository: str, label: str, commit_hash: str) -> str:
    organization = os.getenv("GITHUB_COURSE_ORG", "")
    token = os.getenv("GITHUB_API_TOKEN", "")
    if not token or not ORG_PATTERN.fullmatch(organization):
        raise GitHubVerificationError(
            "La verificación de GitHub requiere configuración del servidor. Avisa al docente."
        )
    if not REPO_PATTERN.fullmatch(repository) or repository in (".", ".."):
        raise GitHubVerificationError("El repositorio asignado al equipo es inválido. Avisa al docente.")
    if label in (".", ".."):
        raise GitHubVerificationError("La etiqueta de GitHub es inválida.")

    repo_path = f"/repos/{organization}/{repository}"
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=5.0,
    ) as client:
        ref = await _get(client, f"{repo_path}/git/ref/tags/{quote(label, safe='')}")
        target = ref.get("object")
        if ref.get("ref") != f"refs/tags/{label}":
            raise GitHubVerificationError("La etiqueta no existe en el repositorio asignado.")

        # Un objeto tag anotado puede apuntar a otro tag antes de llegar al commit.
        for _ in range(5):
            if not isinstance(target, dict):
                break
            sha = target.get("sha")
            if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
                break
            if target.get("type") == "commit":
                if not sha.startswith(commit_hash):
                    raise GitHubVerificationError("La etiqueta de GitHub apunta a otro commit.")
                commit = await _get(client, f"{repo_path}/commits/{commit_hash}")
                if commit.get("sha") != sha:
                    raise GitHubVerificationError("La etiqueta de GitHub apunta a otro commit.")
                return sha
            if target.get("type") != "tag":
                break
            target = (await _get(client, f"{repo_path}/git/tags/{sha}")).get("object")

    raise GitHubVerificationError("La etiqueta de GitHub no se pudo resolver a un commit.")

"""Configure and launch a local installation of the grader."""

import argparse
from getpass import GetPassWarning, getpass
import json
import os
from pathlib import Path
import secrets
import sys
from tempfile import NamedTemporaryFile
import warnings

from src.github import ORG_PATTERN


SETTINGS_PATH = Path(sys.prefix) / "grader-settings.json"
SETTINGS_KEYS = {
    "ADMIN_TOKEN", "GITHUB_COURSE_ORG", "GITHUB_API_TOKEN",
    "STRICT_ROSTER", "GRADER_LOG_PATH",
}


def load_settings() -> dict[str, str]:
    try:
        contents = SETTINGS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    settings = json.loads(contents)
    if not isinstance(settings, dict) or any(
        key not in SETTINGS_KEYS or not isinstance(value, str)
        for key, value in settings.items()
    ):
        raise ValueError("Formato de configuración inválido")
    return settings


def save_settings(settings: dict[str, str]) -> None:
    # A temporary file with restricted permissions keeps the active configuration intact.
    temporary = NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=SETTINGS_PATH.parent, delete=False
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            json.dump(settings, temporary, indent=2)
            temporary.write("\n")
        temporary_path.replace(SETTINGS_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


def choose(prompt: str, options: tuple[str, ...]) -> str:
    while True:
        answer = input(prompt).strip()
        if answer in options:
            return answer
        print("Selecciona una de las opciones indicadas.")


def read_secret(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        return getpass(prompt).strip()


def configure(settings: dict[str, str]) -> str | None:
    if choose("¿Configurar ahora? [1] Sí [Enter] Después: ", ("1", "")) != "1":
        print("Configuración aplazada.")
        return None

    updated = settings.copy()
    generated_token = None
    print("Pulsa Enter para conservar los valores guardados o configurar después.")
    admin = choose("Token docente: [1] Generar [2] Ingresar [Enter] Omitir: ", ("1", "2", ""))
    if admin == "1":
        generated_token = secrets.token_urlsafe(32)
        updated["ADMIN_TOKEN"] = generated_token
    elif admin == "2":
        token = read_secret("Token docente: ")
        if token:
            updated["ADMIN_TOKEN"] = token

    if choose("¿Configurar GitHub? [1] Sí [Enter] Después: ", ("1", "")) == "1":
        while True:
            org = input("Organización GitHub (Enter para conservar): ").strip()
            if not org or ORG_PATTERN.fullmatch(org):
                break
            print("Escribe el nombre de la organización de GitHub.")
        if org:
            updated["GITHUB_COURSE_ORG"] = org
        token = read_secret("Token GitHub de lectura de Contents (Enter para conservar): ")
        if token:
            updated["GITHUB_API_TOKEN"] = token

    if choose("¿Ajustar padrón y logs? [1] Sí [Enter] Después: ", ("1", "")) == "1":
        roster = choose(
            "Padrón estricto: [1] Activar [2] Desactivar [Enter] Conservar: ",
            ("1", "2", ""),
        )
        if roster:
            updated["STRICT_ROSTER"] = "true" if roster == "1" else "false"
        log_path = input("Ruta del log (Enter para conservar): ").strip()
        if log_path:
            updated["GRADER_LOG_PATH"] = log_path

    if updated != settings:
        save_settings(updated)
        print("Configuración local guardada.")
    return generated_token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "start"))
    parser.add_argument("--check-repo", help="Repositorio de GitHub para verificar al arrancar")
    args = parser.parse_args()

    if args.action == "configure" and not sys.stdin.isatty():
        print("Configuración aplazada. Ejecuta configure desde una terminal interactiva.")
        return 0

    try:
        settings = load_settings()
        if args.action == "configure":
            token = configure(settings)
            if token:
                print(f"Token docente generado: {token}")
                print("Cópialo a tu gestor de secretos.")
            return 0
    except (GetPassWarning, EOFError, KeyboardInterrupt):
        print("\nConfiguración cancelada.", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print(f"No se pudo leer o guardar la configuración en {SETTINGS_PATH}.", file=sys.stderr)
        return 1

    for key, value in settings.items():
        os.environ.setdefault(key, value)
    from tools.startup_checks import serve

    return serve(args.check_repo)


if __name__ == "__main__":
    sys.exit(main())

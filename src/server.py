from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, HttpUrl, ValidationError
import json
import logging
import sys

# Import database functions
from src.database import init_db, register_team_db, save_submission_db

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("grader.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Initialize database
try:
    init_db()
    logger.info("Base de datos inicializada correctamente.")
except Exception as e:
    logger.error(f"Error inicializando base de datos: {e}")

# Inicializar FastMCP
mcp = FastMCP("Landivar_Grader_MCP")

class UrlValidator(BaseModel):
    url: HttpUrl

def mask_token(token: str) -> str:
    """Enmascara el token mostrando solo los primeros 4 caracteres."""
    if not token:
        return ""
    if len(token) <= 4:
        return "***"
    return f"{token[:4]}***"

@mcp.tool()
def register_team(section: str, modality: str, members: list[dict]) -> str:
    """
    Registra un equipo de estudiantes y retorna un token único.
    """
    try:
        if section not in ["01", "02"]:
            logger.warning("Intento de registro fallido: section inválida.")
            return "Error de validación: 'section' debe ser estrictamente '01' o '02'."

        if modality not in ["individual", "pareja", "trio"]:
            logger.warning("Intento de registro fallido: modality inválida.")
            return "Error de validación: 'modality' debe ser estrictamente 'individual', 'pareja' o 'trio'."

        for member in members:
            if not isinstance(member, dict):
                logger.warning("Intento de registro fallido: members no contiene diccionarios.")
                return "Error de validación: cada elemento de 'members' debe ser un diccionario."
            if "carnet" not in member or "nombre" not in member:
                logger.warning("Intento de registro fallido: falta carnet o nombre.")
                return "Error de validación: cada miembro debe contener las claves 'carnet' y 'nombre'."

        team_token = register_team_db(section, modality, members)
        logger.info(f"Equipo registrado exitosamente. Token asignado: {mask_token(team_token)} (section: {section}, modality: {modality})")
        return f"Éxito: Equipo registrado correctamente. Guarda tu team_token: {team_token}"
    except Exception as e:
        logger.error(f"Error interno al registrar equipo: {str(e)}")
        return f"Error interno al registrar equipo: {str(e)}"

@mcp.tool()
def submit_lab_results(
    team_token: str,
    deliverable_id: str, 
    repo_url: str, 
    commit_hash: str,
    metrics_json: str, 
    is_ci_passing: bool
) -> str:
    """
    Permite a un equipo enviar los resultados de su laboratorio para calificación.
    """
    masked = mask_token(team_token)
    try:
        # Validate team_token and deliverable_id presence
        if not team_token or not deliverable_id:
            logger.warning("Intento de envío fallido: faltan token o deliverable_id.")
            return "Error de validación: team_token y deliverable_id son obligatorios."

        # Validate repo_url
        try:
            UrlValidator(url=repo_url)
        except ValidationError:
            logger.warning(f"Intento de envío fallido ({masked}): repo_url inválida.")
            return "Error de validación: repo_url debe ser una URL válida."

        # Validate commit_hash presence
        if not commit_hash:
            logger.warning(f"Intento de envío fallido ({masked}): falta commit_hash.")
            return "Error de validación: commit_hash es obligatorio."

        # Validate metrics_json is valid JSON
        try:
            json.loads(metrics_json)
        except json.JSONDecodeError:
            logger.warning(f"Intento de envío fallido ({masked}): metrics_json inválido.")
            return "Error de validación: metrics_json debe ser un JSON válido."
            
        receipt_id = save_submission_db(team_token, deliverable_id, repo_url, commit_hash, metrics_json, is_ci_passing)
        logger.info(f"Resultados recibidos correctamente para el token {masked}. ID confirmación: {receipt_id}")
        return f"Éxito: Resultados recibidos correctamente. ID de confirmación: {receipt_id}"
        
    except ValueError:
        logger.warning(f"Intento de envío fallido: el token {masked} no existe o es inválido.")
        return f"El team_token '{masked}' no existe o es inválido."
    except Exception as e:
        logger.error(f"Error interno al procesar el envío ({masked}): {str(e)}")
        return f"Error interno al procesar el envío: {str(e)}"

if __name__ == "__main__":
    # Ejecuta el servidor usando SSE para conexiones remotas
    logger.info("Iniciando Landívar Grader MCP Server (SSE) en el puerto 8000...")
    mcp.settings.port = 8000
    mcp.settings.host = "0.0.0.0"
    mcp.run(transport='sse')

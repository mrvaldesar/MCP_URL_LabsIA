import logging
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, HttpUrl, ValidationError
import json
import sqlite3
import uuid
from datetime import datetime

# Import database functions
from src.database import init_db, register_team_db, save_submission_db

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("grader.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("grader_mcp")

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

@mcp.tool()
def register_team(section: str, modality: str, members: list[dict]) -> str:
    """
    Registra un equipo de estudiantes y retorna un token único.
    """
    try:
        if section not in ["01", "02"]:
            logger.warning(f"Error de validación en registro: section inválida ({section})")
            return "Error de validación: 'section' debe ser estrictamente '01' o '02'."

        if modality not in ["individual", "pareja", "trio"]:
            logger.warning(f"Error de validación en registro: modality inválida ({modality})")
            return "Error de validación: 'modality' debe ser estrictamente 'individual', 'pareja' o 'trio'."

        for member in members:
            if not isinstance(member, dict):
                logger.warning("Error de validación en registro: miembro no es un diccionario")
                return "Error de validación: cada elemento de 'members' debe ser un diccionario."
            if "carnet" not in member or "nombre" not in member:
                logger.warning("Error de validación en registro: miembro carece de 'carnet' o 'nombre'")
                return "Error de validación: cada miembro debe contener las claves 'carnet' y 'nombre'."

        team_token = register_team_db(section, modality, members)
        logger.info(f"Nuevo equipo registrado: {team_token} (Sección: {section}, Modalidad: {modality})")
        return f"Éxito: Equipo registrado correctamente. Guarda tu team_token: {team_token}"
    except Exception as e:
        logger.exception(f"Error interno al registrar equipo: {e}")
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
    try:
        # Validate team_token and deliverable_id presence
        if not team_token or not deliverable_id:
            logger.warning("Error de validación en envío: team_token o deliverable_id faltante")
            return "Error de validación: team_token y deliverable_id son obligatorios."

        # Validate repo_url
        try:
            UrlValidator(url=repo_url)
        except ValidationError:
            logger.warning(f"Error de validación en envío: repo_url inválida ({repo_url})")
            return "Error de validación: repo_url debe ser una URL válida."

        # Validate commit_hash presence
        if not commit_hash:
            logger.warning("Error de validación en envío: commit_hash faltante")
            return "Error de validación: commit_hash es obligatorio."

        # Validate metrics_json is valid JSON
        try:
            json.loads(metrics_json)
        except json.JSONDecodeError:
            logger.warning("Error de validación en envío: metrics_json no es JSON válido")
            return "Error de validación: metrics_json debe ser un JSON válido."
            
        receipt_id = save_submission_db(team_token, deliverable_id, repo_url, commit_hash, metrics_json, is_ci_passing)
        logger.info(f"Resultados recibidos exitosamente para equipo {team_token}, entregable {deliverable_id}. ID: {receipt_id}")
        return f"Éxito: Resultados recibidos correctamente. ID de confirmación: {receipt_id}"
        
    except ValueError as ve:
        logger.warning(f"Error de validación de negocio en envío: {ve}")
        return str(ve) # Retorna el error si el token no existe
    except Exception as e:
        logger.exception(f"Error interno al procesar el envío: {e}")
        return f"Error interno al procesar el envío: {str(e)}"

if __name__ == "__main__":
    # Ejecuta el servidor usando SSE para conexiones remotas
    logger.info("Iniciando Landívar Grader MCP Server (SSE) en el puerto 8000...")
    mcp.settings.port = 8000
    mcp.settings.host = "0.0.0.0"
    mcp.run(transport='sse')

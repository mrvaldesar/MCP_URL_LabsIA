from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, HttpUrl
import json
import sqlite3
import uuid
from datetime import datetime

# Inicializar FastMCP
mcp = FastMCP("Landivar_Grader_MCP")

def save_to_db(student_id: str, deliverable_id: str, repo_url: str, metrics: str, ci_passing: bool) -> str:
    # TODO: Jules debe implementar la lógica de base de datos aquí
    pass

@mcp.tool()
def submit_lab_results(
    student_id: str, 
    deliverable_id: str, 
    repo_url: str, 
    metrics_json: str, 
    is_ci_passing: bool
) -> str:
    """
    Permite a un estudiante enviar los resultados de su laboratorio para calificación.
    El agente del estudiante debe recopilar las métricas del contrato D<n> y enviarlas aquí.
    """
    try:
        # Validación básica
        if not student_id or not deliverable_id:
            return "Error: student_id y deliverable_id son obligatorios."
            
        receipt_id = save_to_db(student_id, deliverable_id, repo_url, metrics_json, is_ci_passing)
        return f"Éxito: Resultados recibidos correctamente. ID de confirmación: {receipt_id}"
        
    except Exception as e:
        return f"Error interno al procesar el envío: {str(e)}"

if __name__ == "__main__":
    # Ejecuta el servidor usando SSE para conexiones remotas
    print("Iniciando Landívar Grader MCP Server (SSE) en el puerto 8000...")
    # TODO: Jules debe configurar la ejecución con transporte SSE usando Uvicorn/Starlette
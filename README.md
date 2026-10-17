# 🎓 Landívar MCP Grader

Un servidor MCP (Model Context Protocol) remoto y asíncrono diseñado para automatizar la recepción y evaluación de los laboratorios del curso de Inteligencia Artificial (Ingeniería en Informática y Sistemas).

Este servidor expone herramientas (Tools) a través de Server-Sent Events (SSE) para que los agentes de codificación de los estudiantes (Cursor, Claude Desktop, Jules, etc.) puedan registrar equipos y enviar métricas de forma autónoma.

## 🚀 Características Principales
- **Registro Agentic:** Los agentes de IA pueden registrar a los estudiantes y obtener un `team_token` de forma autónoma.
- **Validación Estricta:** Uso de Pydantic para asegurar que todos los envíos cumplan con la estructura de calificación requerida (URLs válidas, JSON parseable, secciones correctas).
- **Transporte Remoto (SSE):** Arquitectura lista para la nube, permitiendo conexiones desde cualquier cliente MCP compatible.
- **Persistencia Segura:** Base de datos relacional en SQLite con manejo de concurrencia web.
- **Auditoría y Trazabilidad:** Sistema de *logging* integrado que registra todas las interacciones, envíos exitosos y errores de validación.

---

## 🛠️ Despliegue e Instalación

### Opción A: Usando Docker (Recomendado para Producción)
1. Construye la imagen de Docker:
   ```bash
   docker build -t landivar-mcp-grader .
Ejecuta el contenedor (montando el volumen de datos para persistencia):

Bash
docker run -d -p 8000:8000 -v $(pwd)/data:/app/data --name mcp_grader landivar-mcp-grader
Opción B: Entorno Local (Desarrollo)
Crea un entorno virtual y actívalo:

Bash
python -m venv .venv
source .venv/bin/activate  # En Windows: .venv\Scripts\activate
Instala las dependencias:

Bash
pip install -r requirements.txt
Ejecuta el servidor:

Bash
python -m src.server
El servidor estará escuchando en http://localhost:8000 (o el dominio configurado). El archivo de base de datos se creará automáticamente en data/grader.db y los registros en grader.log.

🤖 Uso para los Estudiantes
Los estudiantes deben configurar su cliente MCP (ej. claude_desktop_config.json o la configuración de Cursor) para apuntar a la URL de este servidor utilizando el transporte SSE.

Herramientas Disponibles (Tools):
register_team

Descripción: Registra un nuevo equipo en el sistema.

Parámetros: section ("01" o "02"), modality ("individual", "pareja", "trio"), members (lista de diccionarios con carnet y nombre).

Retorno: Un team_token único que el agente debe guardar en las variables de entorno del estudiante.

submit_lab_results

Descripción: Envía los resultados de un entregable (D1 a D10).

Parámetros: team_token, deliverable_id (ej. "D5"), repo_url, commit_hash, metrics_json, is_ci_passing.

Retorno: Mensaje de confirmación y ID de recibo.

📋 Logs y Auditoría
Cualquier fallo de validación (por ejemplo, si un agente envía un token inválido o un formato incorrecto) o errores internos del servidor se registrarán automáticamente en el archivo grader.log en la raíz del proyecto para fines de auditoría.

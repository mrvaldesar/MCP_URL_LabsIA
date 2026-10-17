# Contexto del Proyecto: Landívar MCP Grader

## Propósito
Servidor MCP (Model Context Protocol) remoto en Python. Expone herramientas a los agentes de IA de los estudiantes (Cursor, Claude Desktop, etc.) para que registren sus equipos y envíen automáticamente los resultados de sus entregables de laboratorio (URL, commits, métricas) hacia una base de datos centralizada del docente.

## Stack Tecnológico
- **Lenguaje:** Python 3.10+
- **SDK MCP:** `mcp` (oficial de Anthropic, usando `FastMCP`)
- **Transporte:** SSE (Server-Sent Events) a través de Starlette/FastAPI.
- **Base de Datos:** SQLite (`data/grader.db`).
- **Validación:** `pydantic`.

## Base de Datos
Debe contemplar dos tablas relacionales:
1. `teams`: id, team_token (único), section (01 o 02), modality (individual, pareja, trio), members (JSON), created_at.
2. `submissions`: id, team_id (FK), deliverable_id (ej. D1, D2), repo_url, commit_hash, metrics_json, is_ci_passing, timestamp.

## Herramientas (Tools) a Exponer
El servidor debe exponer estas dos herramientas mediante el decorador `@mcp.tool()`:

1. `register_team(section: str, modality: str, members: list[dict]) -> str`
   - Genera un `team_token` seguro (ej. `secrets.token_hex(16)`).
   - Guarda el equipo en la tabla `teams`.
   - Retorna un mensaje indicando éxito y entregando el `team_token` para que el agente del estudiante lo guarde en su `.env`.

2. `submit_lab_results(team_token: str, deliverable_id: str, repo_url: str, commit_hash: str, metrics_json: str, is_ci_passing: bool) -> str`
   - Valida que el `team_token` exista en la tabla `teams`.
   - Si no existe, retorna un error claro.
   - Si existe, inserta el registro en `submissions` referenciando al equipo.
   - Retorna un ID o mensaje de confirmación exitosa.

## Reglas de Arquitectura para el Agente (Jules)
1. **Transporte SSE:** Inicializa FastMCP configurando host y puerto para exponer endpoints web (SSE y POST para mensajes). NO uses `stdio`.
2. **Resiliencia:** Si faltan datos o el token es inválido, devuelve strings de error descriptivos (no rompas el servidor con excepciones no manejadas) para que el agente del estudiante sepa cómo corregirlo.
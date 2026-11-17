# 🎓 Landívar MCP Grader

Un servidor MCP (Model Context Protocol) remoto y asíncrono diseñado para automatizar la recepción y evaluación de los laboratorios del curso de Inteligencia Artificial (Ingeniería en Informática y Sistemas).

Este servidor expone herramientas (Tools) a través de Server-Sent Events (SSE) para que los agentes de codificación de los estudiantes (Cursor, Claude Desktop, Jules, etc.) puedan registrar equipos y enviar métricas de forma autónoma.

## 🚀 Características Principales
- **Registro Agentic:** Los agentes de IA pueden registrar a los estudiantes y obtener un `team_token` de forma autónoma.
- **Validación Estricta:** Uso de Pydantic para asegurar que todos los envíos cumplan con la estructura de calificación requerida (identificadores de entregable, hashes de commit y JSON válido).
- **Transporte Remoto (SSE):** Arquitectura lista para la nube, permitiendo conexiones desde cualquier cliente MCP compatible.
- **Persistencia Segura:** Base de datos relacional en SQLite con manejo de concurrencia web.
- **Trazabilidad:** Cada envío aceptado genera un recibo con la fecha UTC y el número de intento.

---

## 🛠️ Despliegue e Instalación

### Opción A. Docker

```bash
docker build -t landivar-mcp-grader .
docker run -d -p 8000:8000 -v "$(pwd)/data:/app/data" --name mcp_grader landivar-mcp-grader
```

### Opción B. Entorno local

```bash
python -m venv .venv
source .venv/bin/activate  # En Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m src.server
```

El servidor estará escuchando en http://localhost:8000 (o el dominio configurado). El archivo de base de datos se creará automáticamente en `data/grader.db`.

## 🤖 Uso para los estudiantes

Configura el cliente MCP para conectarse a `/sse` mediante SSE.

1. Llama a `register_team(team_name, section, modality, members)`. Usa la sección `01` o `02` y proporciona uno, dos o tres integrantes según la modalidad `individual`, `pareja` o `trio`. Cada integrante requiere `github_user` y un `carnet` de siete dígitos.
2. La herramienta devuelve el token una sola vez. Configura el cliente MCP para enviarlo en el encabezado HTTP `Authorization: Bearer <token>`. El agente no lee ni escribe archivos `.env`.
3. Llama a `submit_lab_results(deliverable_id, label, commit_hash, metrics_json, is_ci_passing)`. El identificador va de `D1` a `D10`, el commit usa entre 7 y 40 dígitos hexadecimales en minúscula y las métricas se envían como una cadena JSON válida de hasta 64 KiB en UTF-8.

El recibo incluye `receipt_id`, `deliverable_id`, `label`, `received_at_utc`, `status`, `attempt` y `section`. Las métricas y el estado de CI son declaraciones del equipo. El estado `received` confirma la recepción.

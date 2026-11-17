# Landívar MCP Grader

Servidor remoto en Python 3.10+ con FastMCP, transporte SSE, Pydantic y SQLite en `data/grader.db`. Arranca con `python -m src.server` y expone `/sse` y `/messages/`.

## Contrato MCP

- `register_team(team_name, section, modality, members: list[Member]) -> str` retorna el token del equipo una sola vez. `Member` tiene los campos `github_user` y `carnet` como cadenas. Las secciones son `01` y `02`. Las modalidades `individual`, `pareja` y `trio` requieren uno, dos y tres integrantes, respectivamente.
- El carné tiene siete dígitos ASCII. El usuario GitHub tiene hasta 39 caracteres ASCII alfanuméricos y admite guiones simples entre ellos. El nombre del equipo, los usuarios GitHub y los carnés son únicos globalmente. Los nombres y usuarios se comparan sin distinguir mayúsculas ASCII de minúsculas.
- `submit_lab_results(deliverable_id, label, commit_hash, metrics_json, is_ci_passing) -> Receipt` recibe un entregable de `D1` a `D10`, una etiqueta no vacía, un hash de 7 a 40 dígitos hexadecimales en minúscula y una cadena JSON válida de hasta 64 KiB en UTF-8. Las métricas y el estado de CI son datos declarados por el equipo.
- El recibo contiene `receipt_id`, `deliverable_id`, `label`, `received_at_utc`, `status`, `attempt` y `section`. El estado inicial es `received` y el contador empieza en uno por equipo y entregable. Los errores de validación o autenticación se devuelven como errores de herramienta MCP.

## Política del token

El estudiante configura el cliente MCP para enviar `Authorization: Bearer <token>` en las peticiones HTTP. El token queda fuera de los argumentos de las herramientas. El agente no lee ni escribe archivos `.env` ni guarda el token en el repositorio.

SQLite conserva `token_hash` y `salt` en `teams`; la verificación emplea `hmac.compare_digest`. Evita registrar o incluir el token en mensajes de error. La migración de bases antiguas conserva los IDs, envíos y vigencia de los tokens. Los equipos antiguos carecen de nombre de equipo y usuario GitHub, datos que la versión anterior no recopilaba.

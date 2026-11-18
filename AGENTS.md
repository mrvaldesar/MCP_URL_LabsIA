# Landívar MCP Grader

Servidor remoto en Python 3.10+ con FastMCP, transporte Streamable HTTP, Pydantic y SQLite en `data/grader.db`. Arranca con `python -m src.server` y expone `/mcp`.

## Contrato MCP

- `register_team(team_name, section, modality, members: list[Member]) -> str` retorna una confirmación. El servidor emite la credencial una sola vez en el encabezado HTTP de respuesta `X-Team-Token`. `Member` tiene los campos `github_user` y `carnet` como cadenas. Las secciones son `01` y `02`. Las modalidades `individual`, `pareja` y `trio` requieren uno, dos y tres integrantes, respectivamente.
- El carné tiene siete dígitos ASCII. El usuario GitHub tiene hasta 39 caracteres ASCII alfanuméricos y admite guiones simples entre ellos. El nombre del equipo, los usuarios GitHub y los carnés son únicos globalmente. Los nombres y usuarios se comparan sin distinguir mayúsculas ASCII de minúsculas.
- `submit_lab_results(deliverable_id, label, commit_hash, metrics_json, is_ci_passing) -> Receipt` recibe un entregable de `D1` a `D10`, una etiqueta no vacía, un hash de 7 a 40 dígitos hexadecimales en minúscula y una cadena JSON válida de hasta 64 KiB en UTF-8. Las métricas y el estado de CI son datos declarados por el equipo.
- El recibo contiene `receipt_id`, `deliverable_id`, `label`, `received_at_utc`, `status`, `attempt` y `section`. El estado inicial es `received` y el contador empieza en uno por equipo y entregable. Las herramientas declaran `readOnlyHint=false`, `idempotentHint=false` y `destructiveHint=false`. Los errores de validación se devuelven como errores de herramienta MCP. La autenticación ausente o inválida devuelve HTTP 401.

## Política del token

El registro y el descubrimiento MCP son públicos. El estudiante recoge `X-Team-Token` por HTTP y configura el cliente MCP para enviar `Authorization: Bearer <token>`. El middleware resuelve el equipo y pasa solo su ID al servidor MCP. El token queda fuera de los argumentos y del resultado MCP. Las rutas de descubrimiento OAuth devuelven 404. El agente no lee ni escribe archivos `.env` ni guarda el token en el repositorio.

SQLite conserva `token_hash` y `salt` en `teams`; la verificación emplea `hmac.compare_digest`. Evita registrar o incluir el token en mensajes de error. La migración de bases antiguas conserva los IDs, envíos y vigencia de los tokens. Los equipos antiguos carecen de nombre de equipo y usuario GitHub, datos que la versión anterior no recopilaba.

## Operación docente

Las rutas HTTP de administración quedan fuera del listado de herramientas MCP. Configura un `ADMIN_TOKEN` independiente en el entorno del servidor y envíalo como `Authorization: Bearer <ADMIN_TOKEN>`. Un token de equipo recibe 403.

- `POST /admin/teams/{id}/rotate-token` reemplaza la credencial del equipo y entrega la nueva en `X-Team-Token`.
- `GET /admin/export?section=01` descarga un CSV de envíos de la sección `01` o `02`.
- `POST /admin/roster` recibe `{"section":"01","members":[{"github_user":"alumna-uno","carnet":"1234524"}]}`. Reemplaza atómicamente el padrón de esa sección para consulta operativa. El registro MCP conserva sus propias validaciones.
- `GET /health` comprueba SQLite. El registro MCP permite cinco solicitudes por IP cada diez minutos y devuelve 429 con `Retry-After` al superar el límite. Las entregas permiten diez solicitudes por token de equipo cada minuto y devuelven un error de herramienta MCP con `Retry-After` al superar el límite.

El arranque aplica las migraciones numeradas de `migrations/` y guarda su historial en `schema_migrations`. Escribe eventos JSON en `grader.log` o en `GRADER_LOG_PATH` si está configurado. Mantén las credenciales fuera de los logs.

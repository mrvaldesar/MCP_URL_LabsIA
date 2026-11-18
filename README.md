# 🎓 Landívar MCP Grader

Un servidor MCP (Model Context Protocol) remoto y asíncrono diseñado para automatizar la recepción y evaluación de los laboratorios del curso de Inteligencia Artificial (Ingeniería en Informática y Sistemas).

Este servidor expone herramientas (Tools) mediante Streamable HTTP para registrar equipos desde una terminal y enviar métricas desde clientes MCP como Cursor, Claude Desktop y Jules.

## 🚀 Características Principales
- **Registro de equipos:** Los estudiantes obtienen su credencial desde una terminal antes de conectar su cliente MCP.
- **Validación Estricta:** Uso de Pydantic para asegurar que todos los envíos cumplan con la estructura de calificación requerida (identificadores de entregable, hashes de commit y JSON válido).
- **Transporte remoto (Streamable HTTP):** Arquitectura lista para la nube, permitiendo conexiones desde cualquier cliente MCP compatible.
- **Persistencia Segura:** Base de datos relacional en SQLite con manejo de concurrencia web.
- **Trazabilidad:** Cada envío aceptado genera un recibo con la fecha UTC y el número de intento.

---

## 🛠️ Despliegue e Instalación

### Opción A. Docker

```bash
mkdir -p data
sudo chown 10001:10001 data
docker build -t landivar-mcp-grader .
docker run -d -p 8000:8000 -v "$(pwd)/data:/app/data" --name mcp_grader landivar-mcp-grader
```

La imagen ejecuta el servicio con UID y GID `10001`. Si el volumen montado ya contiene una base, ese usuario necesita acceso de lectura y escritura al directorio y a los archivos existentes de SQLite y del log.

### Opción B. Entorno local

```bash
python -m venv .venv
source .venv/bin/activate  # En Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m src.server
```

El servidor estará escuchando en http://localhost:8000 (o el dominio configurado). El archivo de base de datos se creará automáticamente en `data/grader.db`.

### Arranque en frío y suspensión

La capa gratuita de Render suspende servicios inactivos. En Railway, el servicio puede dormir si está habilitada la suspensión por inactividad (Serverless). El arranque en frío puede provocar timeouts generalizados en los clientes MCP durante una entrega. Configura un monitor externo que haga ping a `GET /health` antes y durante la ventana de entrega, con un intervalo menor al tiempo de inactividad de la plataforma si el plan lo permite. Si el plan sigue suspendiendo el servicio, usa una opción sin suspensión durante esas ventanas. [Runbook de operación](docs/runbook.md)

### Verificación de entregas en GitHub

La etiqueta publicada en GitHub es la entrega oficial. El servidor registra el recibo. Usa una organización para las dos secciones y un repositorio privado por equipo, con nombre corto `lab-ia-s01-<equipo>` o `lab-ia-s02-<equipo>`. El argumento `github_repository` es obligatorio en el registro.

Una indisponibilidad de GitHub durante la entrega genera un recibo con `verification_status=pendiente`. Conserva el recibo para que el docente pueda reintentar la verificación. El CSV incluye el CI declarado y `verified_ci_status`, que refleja la consulta a GitHub al verificar el recibo.

## 🤖 Uso para los estudiantes

**Antes de conectar el LLM, ejecuta manualmente `curl -i` en tu terminal** con el ejemplo de registro de abajo. El servidor devuelve `X-Team-Token` una sola vez como encabezado HTTP. El agente puede mostrar solo la confirmación MCP y perder el token si realiza el registro por ti.

1. En la solicitud de `register_team`, usa la sección `01` o `02` y proporciona uno, dos o tres integrantes según la modalidad `individual`, `pareja` o `trio`. Cada integrante requiere `github_user` y un `carnet` como texto en el formato del curso (siete dígitos por defecto) y debe aparecer en el padrón de su sección.
2. Guarda el valor de `X-Team-Token` en privado. Configura el cliente MCP para conectarse a `/mcp` mediante Streamable HTTP con el encabezado `Authorization: Bearer <token>`. Conecta el LLM después de configurar la credencial.
3. Llama a `submit_lab_results` con `deliverable_id`, `commit_hash`, `metrics_json` e `is_ci_passing`. El argumento opcional `label` usa `deliverable_id` por defecto. El identificador va de `D1` a `D10`, el commit usa entre 7 y 40 dígitos hexadecimales en minúscula y las métricas se envían como una cadena JSON válida de hasta 64 KiB en UTF-8. Publica en el repositorio asignado una etiqueta Git `label` idéntica a `deliverable_id` (por ejemplo, `D1`) antes de enviar. GitHub debe resolver el hash declarado al mismo commit al que apunta la etiqueta. Las métricas y el estado de CI siguen siendo declaraciones del equipo.

El recibo incluye el ID, el instante UTC, el intento, la sección, la verificación, el CI verificado y la puntualidad. El estado `received` confirma la recepción. Las entregas tardías se aceptan y quedan marcadas.

Usa la URL HTTPS del curso en lugar de `http://localhost:8000` para el registro remoto.

```bash
curl -i http://localhost:8000/mcp \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"register_team","arguments":{"team_name":"Equipo Uno","section":"01","modality":"individual","github_repository":"lab-ia-s01-uno","members":[{"github_user":"alumna-uno","carnet":"1234524"}]}}}'
```

## Operación docente

Configura `ADMIN_TOKEN` en el entorno del servidor con una credencial independiente de los tokens de equipo. En Docker, añade `-e ADMIN_TOKEN` al comando de arranque para pasar la variable desde el entorno del operador. Las rutas administrativas reciben `Authorization: Bearer <token>`. `ADMIN_TOKEN` conserva acceso a todas las secciones. Configura credenciales individuales con `TEACHER_ADMIN_CREDENTIALS`. El log registra el docente, la acción, el recurso y el resultado.

| Ruta | Operación |
| --- | --- |
| `GET /health` | Comprueba SQLite y que las migraciones aplicadas coincidan con los archivos del despliegue. Devuelve 503 si falla la comprobación. Disponible sin token. |
| `POST /admin/teams/{id}/rotate-token` | Invalida la credencial anterior y entrega la nueva en `X-Team-Token`. El ID del equipo aparece en el CSV. |
| `POST /admin/teams/{id}/update` | Actualiza los integrantes o la modalidad y entrega una credencial nueva en `X-Team-Token`. |
| `GET /admin/export?section=01` | Descarga los envíos de la sección indicada como CSV. |
| `GET /admin/teams[?section=01]` | Lista los equipos a los que tiene acceso el docente. |
| `PATCH /admin/teams/{id}/assignment` | Corrige el repositorio, cambia la sección o actualiza ambos en una operación. |
| `PUT /admin/windows` | Configura o reabre una ventana de entrega por sección y entregable. |
| `POST /admin/submissions/{id}/retry-verification` | Reintenta la verificación de un recibo pendiente. |
| `GET /admin/github/health` | Comprueba si la credencial permite listar repositorios privados de la organización. |
| `POST /admin/roster` | Reemplaza el padrón de una sección con un JSON que contiene `section` y `members`. |

Ejemplo de cuerpo para el padrón:

```json
{"section":"01","members":[{"github_user":"alumna-uno","carnet":"1234524"}]}
```

Carga el padrón de ambas secciones antes de abrir el registro. Por defecto se exige que el usuario GitHub y el carné de cada integrante coincidan con la misma entrada del padrón de su sección. El servidor avisa al arrancar si el padrón está vacío.

Para actualizar un equipo, envía `members` con la lista completa de integrantes, `modality` o ambos campos. Si envías solo `members`, la modalidad se deduce de la cantidad de integrantes. Si envías solo `modality`, debe corresponder a la cantidad de integrantes que ya tiene el equipo. Una actualización exitosa invalida la credencial anterior.

### Configuración

| Variable | Valor predeterminado | Efecto |
| --- | --- | --- |
| `STRICT_ROSTER` | Activo | Solo se desactiva con `false`, `0`, `off` o `no`. |
| `CARNET_PATTERN` | `^[0-9]{7}$` | Expresión regular Python aplicada al carné completo en registros y padrón, con un máximo de 64 caracteres. |
| `REGISTER_IP_LIMIT` | `80` | Intentos de registro por IP. Permite 64 equipos en una red compartida y 16 intentos adicionales. |
| `REGISTER_WINDOW_SECONDS` | `600` | Ventana móvil común de los límites de registro. |
| `REGISTER_GLOBAL_LIMIT` | `256` | Intentos totales por proceso en la ventana, cuatro veces el tamaño esperado de la cohorte. |
| `REGISTER_CARNET_FAILURE_LIMIT` | `5` | Intentos sin coincidencia en el padrón estricto antes de limitar el par IP y carné durante la ventana. |
| `TRUSTED_PROXY_CIDRS` | Vacío | Redes de proxies autorizados a proporcionar `X-Forwarded-For`, separadas por comas. |
| `TEACHER_ADMIN_CREDENTIALS` | `[]` | Lista JSON de docentes con identificador, sección y SHA-256 de su token. |
| `GITHUB_COURSE_ORG` | Vacío | Organización única del curso. |
| `GITHUB_API_TOKEN` | Vacío | Token de grano fino alternativo. Se prefiere una GitHub App con los tres campos del runbook. |
| `ADMIN_TOKEN` | Sin configurar | Credencial de superadministración. |
| `GRADER_LOG_PATH` | `grader.log` local, `/app/data/grader.log` en Docker | Ruta del log de auditoría. |

Los límites viven en la memoria de cada proceso. Ejecuta una réplica. Los envíos conservan el límite de diez llamadas por token cada minuto.

Para exportar desde una copia local de la base, ejecuta desde la raíz del repositorio:

```bash
python -m tools.export_section 01 --db data/grader.db > seccion-01.csv
```


## Referencia MCP

El [JSON de tools/list](docs/tools-list.json) se genera desde el servidor local con `python -m tools.generate_tool_reference > docs/tools-list.json`. Las firmas publicadas se mantienen estables durante el semestre. Una prueba compara la referencia con la respuesta HTTP del servidor.

Consulta el [runbook](docs/runbook.md) para configurar el proxy, las credenciales, las ventanas, el respaldo y el procedimiento de actualización.

La referencia generada lleva `interface_version`. La versión 2 requiere `github_repository` en `register_team`. El campo de versión pertenece al artefacto de documentación. Para publicar un cambio de firma autorizado, incrementa `INTERFACE_VERSION` en el generador, regenera el JSON y añade la huella de la nueva versión en `INTERFACE_SIGNATURES` en las pruebas. Conserva las huellas de versiones ya publicadas.

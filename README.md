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

### Opción B. Instaladores para Windows y Ubuntu

Descarga el repositorio completo y abre una terminal en la carpeta que contiene
`requirements.txt`. Los instaladores crean un entorno virtual `.venv` e instalan
las dependencias del servidor con las mismas versiones fijadas que usa Docker.
Puedes volver a ejecutar el instalador para actualizar las dependencias después
de actualizar el repositorio.

**Windows (PowerShell)**

```powershell
.\install-windows.cmd
.\.venv\Scripts\python.exe -m src.server
```

El instalador usa Python 3.10 o superior si ya está disponible. Si falta,
intenta instalar Python 3.12 para el usuario mediante WinGet.

**Ubuntu**

```bash
bash install-ubuntu.sh
.venv/bin/python -m src.server
```

Si faltan los paquetes de Python y del entorno virtual, el instalador intenta
instalarlos con `sudo apt-get`.

Con el servidor en ejecución, abre <http://localhost:8000/health> para comprobar
el estado. El endpoint MCP local es `http://localhost:8000/mcp` y la base de datos
se crea automáticamente en `data/grader.db`. Para conectarte al servidor del
curso como estudiante, sigue la sección «Uso para los estudiantes».

### Configuración local del servidor

Con Python y las dependencias instaladas puedes arrancar el servidor, comprobar
`/health` y registrar equipos. Para usar el servidor durante el curso, configura
estas variables de entorno antes de iniciarlo:

| Variable | Cuándo se necesita | Valor |
| --- | --- | --- |
| `GITHUB_COURSE_ORG` | Recepción de entregas | Nombre de la organización de GitHub del curso. |
| `GITHUB_API_TOKEN` | Recepción de entregas | Token de GitHub del servidor con permiso de lectura de **Contents** en los repositorios asignados. |
| `ADMIN_TOKEN` | Operación docente | Credencial independiente para exportar, gestionar padrones y administrar equipos. |
| `STRICT_ROSTER` | Opcional | `true` para exigir coincidencia con el padrón. Carga el padrón de cada sección antes de habilitarlo. |
| `GRADER_LOG_PATH` | Opcional | Ruta del log. En local usa `grader.log` de forma predeterminada. |

Para habilitar entregas y administración en una sesión local, ejecuta el bloque
de tu sistema después de instalar las dependencias. Sustituye `organizacion-del-curso`
y carga las credenciales desde tu gestor de secretos cuando se te soliciten.
Puedes generar una credencial administrativa aleatoria con
`python -c "import secrets; print(secrets.token_urlsafe(32))"` usando el
ejecutable de Python del entorno virtual. Guárdala en tu gestor de secretos.

**Windows (PowerShell)**

```powershell
$env:GITHUB_COURSE_ORG = 'organizacion-del-curso'
$env:GITHUB_API_TOKEN = [pscredential]::new('github', (Read-Host 'Token de GitHub' -AsSecureString)).GetNetworkCredential().Password
$env:ADMIN_TOKEN = [pscredential]::new('admin', (Read-Host 'Token de administracion' -AsSecureString)).GetNetworkCredential().Password
.\.venv\Scripts\python.exe -m src.server
```

**Ubuntu (Bash)**

```bash
export GITHUB_COURSE_ORG='organizacion-del-curso'
read -rsp 'Token de GitHub: ' GITHUB_API_TOKEN
echo
read -rsp 'Token de administracion: ' ADMIN_TOKEN
echo
export GITHUB_API_TOKEN ADMIN_TOKEN
.venv/bin/python -m src.server
```

Los valores de estos ejemplos se cargan en la sesión actual. Para un despliegue,
inyéctalos desde el gestor de secretos de la plataforma. El arranque local lee
el entorno del proceso. Mantén las credenciales fuera del repositorio.

Después del registro, [asigna un repositorio a cada equipo](#verificación-de-entregas-en-github)
para habilitar sus entregas. Cada equipo usa el `X-Team-Token` que recibe al
registrarse para configurar su cliente MCP.

### Arranque en frío y suspensión

La capa gratuita de Render suspende servicios inactivos. En Railway, el servicio puede dormir si está habilitada la suspensión por inactividad (Serverless). El arranque en frío puede provocar timeouts generalizados en los clientes MCP durante una entrega. Configura un monitor externo que haga ping a `GET /health` antes y durante la ventana de entrega, con un intervalo menor al tiempo de inactividad de la plataforma si el plan lo permite. Si el plan sigue suspendiendo el servicio, usa una opción sin suspensión durante esas ventanas. [Runbook de operación](docs/runbook.md)

### Verificación de entregas en GitHub

Configura `GITHUB_COURSE_ORG` con el nombre de la organización del curso y
`GITHUB_API_TOKEN` con un token del servidor que tenga acceso de lectura a
**Contents** en los repositorios de los equipos. Carga el token desde el gestor
de secretos. En Docker, pasa ambas variables con
`-e GITHUB_COURSE_ORG -e GITHUB_API_TOKEN`.

Después del registro, el operador asigna a cada equipo su repositorio en la
base de datos persistente. Guarda solo el nombre del repositorio dentro de la
organización configurada. Por ejemplo, para asignar `equipo-42` al equipo con
ID `42` después de que el servidor aplique las migraciones:

```bash
sqlite3 -bail data/grader.db <<'SQL'
SELECT id, team_name, section, github_repository FROM teams ORDER BY id;
SQL

sqlite3 -bail data/grader.db <<'SQL'
UPDATE teams SET github_repository = 'equipo-42' WHERE id = 42;
SELECT id, github_repository FROM teams WHERE id = 42;
SQL
```

Cada repositorio puede pertenecer a un solo equipo. Los equipos existentes
también requieren una asignación antes de enviar nuevas entregas.

## 🤖 Uso para los estudiantes

**Antes de conectar el LLM, ejecuta manualmente `curl -i` en tu terminal** con el ejemplo de registro de abajo. El servidor devuelve `X-Team-Token` una sola vez como encabezado HTTP. El agente puede mostrar solo la confirmación MCP y perder el token si realiza el registro por ti.

1. En la solicitud de `register_team`, usa la sección `01` o `02` y proporciona uno, dos o tres integrantes según la modalidad `individual`, `pareja` o `trio`. Cada integrante requiere `github_user` y un `carnet` de siete dígitos.
2. Guarda el valor de `X-Team-Token` en privado. Configura el cliente MCP para conectarse a `/mcp` mediante Streamable HTTP con el encabezado `Authorization: Bearer <token>`. Conecta el LLM después de configurar la credencial.
3. Llama a `submit_lab_results(deliverable_id, label, commit_hash, metrics_json, is_ci_passing)`. El identificador va de `D1` a `D10`, el commit usa entre 7 y 40 dígitos hexadecimales en minúscula y las métricas se envían como una cadena JSON válida de hasta 64 KiB en UTF-8. Publica en el repositorio asignado una etiqueta Git `label` idéntica a `deliverable_id` (por ejemplo, `D1`) antes de enviar. GitHub debe resolver el hash declarado al mismo commit al que apunta la etiqueta. Las métricas y el estado de CI siguen siendo declaraciones del equipo.

El recibo incluye `receipt_id`, `deliverable_id`, `label`, `received_at_utc`, `status`, `attempt` y `section`. Las métricas y el estado de CI son declaraciones del equipo. El estado `received` confirma la recepción.

Usa la URL HTTPS del curso en lugar de `http://localhost:8000` para el registro remoto.

```bash
curl -i http://localhost:8000/mcp \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"register_team","arguments":{"team_name":"Equipo Uno","section":"01","modality":"individual","members":[{"github_user":"alumna-uno","carnet":"1234524"}]}}}'
```

## Operación docente

Configura `ADMIN_TOKEN` en el entorno del servidor con una credencial independiente de los tokens de equipo. En Docker, añade `-e ADMIN_TOKEN` al comando de arranque para pasar la variable desde el entorno del operador. Las rutas administrativas reciben `Authorization: Bearer <ADMIN_TOKEN>`.

| Ruta | Operación |
| --- | --- |
| `GET /health` | Comprueba SQLite y que las migraciones aplicadas coincidan con los archivos del despliegue. Devuelve 503 si falla la comprobación. Disponible sin token. |
| `POST /admin/teams/{id}/rotate-token` | Invalida la credencial anterior y entrega la nueva en `X-Team-Token`. El ID del equipo aparece en el CSV. |
| `POST /admin/teams/{id}/update` | Actualiza los integrantes o la modalidad y entrega una credencial nueva en `X-Team-Token`. |
| `GET /admin/export?section=01` | Descarga los envíos de la sección indicada como CSV. |
| `POST /admin/roster` | Reemplaza el padrón de una sección con un JSON que contiene `section` y `members`. |

Ejemplo de cuerpo para el padrón:

```json
{"section":"01","members":[{"github_user":"alumna-uno","carnet":"1234524"}]}
```

Configura `STRICT_ROSTER=true` para exigir que el usuario GitHub y el carné de cada integrante coincidan con una misma entrada del padrón de su sección. El modo predeterminado permite registros fuera del padrón y deja una advertencia en el log.

Para actualizar un equipo, envía `members` con la lista completa de integrantes, `modality` o ambos campos. Si envías solo `members`, la modalidad se deduce de la cantidad de integrantes. Si envías solo `modality`, debe corresponder a la cantidad de integrantes que ya tiene el equipo. Una actualización exitosa invalida la credencial anterior.

El servidor permite hasta cinco registros por IP en diez minutos y hasta diez llamadas a `submit_lab_results` por token de equipo en un minuto. Cuenta las solicitudes en ventanas móviles en memoria. Cada proceso mantiene su propio conteo y lo reinicia al arrancar. Aplica las migraciones al arrancar y escribe logs JSON en `grader.log` en local y en `/app/data/grader.log` en Docker. Configura `GRADER_LOG_PATH` para cambiar la ruta, por ejemplo a `/app/data/grader.log` para usar el volumen persistente de Docker.

Para exportar desde una copia local de la base, ejecuta desde la raíz del repositorio:

```bash
python -m tools.export_section 01 --db data/grader.db > seccion-01.csv
```

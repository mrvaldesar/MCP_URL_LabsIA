# Runbook de operación docente

Usa la URL HTTPS del servidor en `BASE_URL` y carga `ADMIN_TOKEN` desde el gestor de secretos del operador. El servicio usa `data/grader.db` por defecto. Ejecuta los comandos SQLite sobre el archivo real del volumen persistente.

```bash
BASE_URL=https://grader.example.edu
: "${ADMIN_TOKEN:?Carga ADMIN_TOKEN en la sesión del operador}"
umask 077

admin_curl() {
  printf 'header = "Authorization: Bearer %s"\n' "$ADMIN_TOKEN" |
    curl --config - --fail-with-body --silent --show-error "$@"
}

curl --fail --silent --show-error "$BASE_URL/health"
```

Las rutas administrativas devuelven 403 si falta el token de administración o se usa una credencial de estudiante.

## Registro de estudiantes antes de conectar el LLM

Pide a cada equipo que ejecute manualmente el `curl -i` del README contra la URL HTTPS del curso, antes de conectar el LLM. El registro devuelve `X-Team-Token` una sola vez en los encabezados HTTP. Si el agente realiza el registro, puede mostrar solo el resultado MCP y el estudiante puede perder el acceso al token. El estudiante guarda la credencial en privado y configura `Authorization: Bearer <token>` en el cliente MCP antes de conectarlo.

## Arranque en frío y suspensión

Render suspende por inactividad los servicios gratuitos. Railway puede hacer lo mismo si el servicio tiene activada la opción Serverless. Las primeras peticiones tras la suspensión tendrán que esperar el arranque y pueden causar timeouts generalizados en los clientes MCP de los alumnos.

Configura un monitor externo para enviar `GET /health` antes de abrir la ventana de entrega y durante toda la ventana. Si el plan permite mantener el servicio activo con tráfico, usa un intervalo menor al umbral de inactividad. Verifica que el monitor reciba HTTP 200 en la URL pública antes de abrir las entregas. Un HTTP 503 indica un fallo de SQLite o una diferencia entre las migraciones aplicadas y los archivos del despliegue. Si el plan suspende el servicio a pesar de los pings, usa un plan sin suspensión durante las entregas.

## Configuración de Volúmenes en Producción

SQLite guarda los equipos y las entregas en `data/grader.db`. Sin un volumen persistente, cada redespliegue que reemplace el contenedor borrará los equipos y las entregas registrados. Al arrancar, el servicio creará una base vacía.

Con el `Dockerfile` del repositorio, monta el mismo disco persistente de Render o volumen de Railway en `/app/data` en cada despliegue. Monta el directorio completo para que SQLite pueda guardar `grader.db` y sus archivos auxiliares `grader.db-wal` y `grader.db-shm` en el mismo volumen. El servicio se ejecuta como UID/GID `10001:10001`. Ese usuario debe poder leer y escribir en el directorio y en los archivos existentes de SQLite y del log. Mantén una sola réplica del servicio con este volumen.

Si ya hay registros en el almacenamiento efímero, detén las escrituras y haz una copia con `sqlite3 .backup` antes del primer redespliegue. Restaura la copia en el volumen siguiendo el procedimiento de este runbook. Tras el despliegue, comprueba `/health` y exporta una sección conocida para verificar que se conservaron los registros.

## Rotar un token

Confirma el ID del equipo en la exportación o en la base de datos. La rotación devuelve la nueva credencial en el encabezado HTTP `X-Team-Token` e invalida la anterior.

```bash
team_id=42
rotation_headers=$(mktemp)
admin_curl --request POST --dump-header "$rotation_headers" \
  "$BASE_URL/admin/teams/$team_id/rotate-token"
```

Entrega el valor de `X-Team-Token` por el canal privado acordado. El estudiante actualiza el encabezado `Authorization: Bearer` en su cliente MCP. Elimina el archivo temporal con `rm -- "$rotation_headers"` cuando termines.

## Exportar una sección

Por HTTP, con el servidor en funcionamiento:

```bash
admin_curl --output seccion-01.csv "$BASE_URL/admin/export?section=01"
```

Desde la raíz del repositorio, contra una base de solo lectura:

```bash
python -m tools.export_section 01 --db data/grader.db > seccion-01.csv
```

Cambia `01` por `02` según corresponda. El CSV contiene identificadores de equipo y recibo, integrantes, intentos y métricas declaradas. Guárdalo en un lugar con acceso restringido.

## Borrar un registro de equipo

La operación elimina el equipo y sus envíos. Conserva el padrón docente. Detén el servicio, haz una copia antes de borrar y confirma la sección, el nombre y el ID en la consulta inicial. Si necesitas borrar solo un envío, usa su ID de recibo en `DELETE FROM submissions WHERE id = :receipt_id` dentro de una transacción y conserva el equipo.

```bash
db=data/grader.db
backup="grader-before-delete-$(date -u +%Y%m%dT%H%M%SZ).db"
sqlite3 -bail "$db" ".backup '$backup'"
sqlite3 -bail -readonly "$db" 'SELECT id, team_name, section FROM teams ORDER BY id;'
```

En el ejemplo siguiente, cambia `42` por el ID confirmado. La transacción libera los carnés y usuarios GitHub del equipo eliminado cuando ningún otro equipo los conserva.

```bash
sqlite3 -bail "$db" <<'SQL'
.parameter init
.parameter set :team_id 42
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE removed_members AS
SELECT member.value AS details
FROM teams, json_each(teams.members) AS member
WHERE teams.id = :team_id;
DELETE FROM submissions WHERE team_id = :team_id;
DELETE FROM teams WHERE id = :team_id;
DELETE FROM member_identities
WHERE EXISTS (
  SELECT 1 FROM removed_members
  WHERE CAST(json_extract(details, '$.carnet') AS TEXT) = member_identities.carnet
     OR json_extract(details, '$.github_user') = member_identities.github_user COLLATE NOCASE
)
AND NOT EXISTS (
  SELECT 1 FROM teams, json_each(teams.members) AS member
  WHERE CAST(json_extract(member.value, '$.carnet') AS TEXT) = member_identities.carnet
     OR json_extract(member.value, '$.github_user') = member_identities.github_user COLLATE NOCASE
);
COMMIT;
PRAGMA foreign_key_check;
SQL
```

La última comprobación debe quedar vacía. Reinicia el servicio y verifica `/health`. El contador de intentos se calcula con los envíos que quedan. Si borras el último intento de un entregable y el equipo reenvía, se puede reutilizar ese número de intento.

## Restaurar la base de datos

1. Detén todos los procesos que usan SQLite. Para el contenedor del README, ejecuta `docker stop mcp_grader`.
2. Verifica la copia, prepara la restauración y aparta la base actual junto con sus archivos WAL y SHM. Sustituye el valor de `backup` por la copia elegida.

```bash
set -eu
umask 077
db=data/grader.db
backup=grader-before-delete-20261117T120000Z.db
test -s "$backup"
test "$(sqlite3 -bail -readonly "$backup" 'PRAGMA integrity_check;')" = ok
test -z "$(sqlite3 -bail -readonly "$backup" 'PRAGMA foreign_key_check;')"

recovery=$(mktemp -d "$(dirname "$db")/.grader-recovery.XXXXXX")
sqlite3 -bail -readonly "$backup" ".backup '$recovery/restored.db'"
for file in "$db" "$db-wal" "$db-shm"; do
  if [ -e "$file" ]; then
    mv -- "$file" "$recovery/"
  fi
done
mv -- "$recovery/restored.db" "$db"
printf 'Archivos anteriores conservados en %s\n' "$recovery"
```

3. Confirma que el usuario del servicio puede leer y escribir la base y su directorio. Reinicia el servicio para que aplique las migraciones pendientes. Verifica `/health` y exporta una sección conocida.
4. Si falla la comprobación, detén el servicio y aparta la base restaurada y sus archivos auxiliares. Devuelve desde el directorio de recuperación los archivos originales `grader.db`, `grader.db-wal` y `grader.db-shm` que existían, y vuelve a arrancar.

Restaurar una copia anterior también restaura el estado de las credenciales de esa fecha. Rota los tokens afectados antes de reabrir el servicio al curso.

## Comprobaciones antes de la semana 1

```bash
python -m pip install -r requirements.txt
pytest -q
pytest --cov
```

## Configurar credenciales y proxy

Usa un token de grano fino del propietario de la organización del curso, limitado a los repositorios asignados. Otorga lectura de **Contents**, **Checks** y **Commit statuses**. La consulta de colaboradores es opcional y depende de los permisos de repositorio. Si faltan, el registro conserva su resultado. Guarda el token en el gestor de secretos como `GITHUB_API_TOKEN`. Configura una vigencia de 30 días o el máximo inferior que imponga la organización, registra la fecha efectiva de expiración en el gestor y programa la rotación siete días antes. Para una GitHub App, la credencial equivalente es un token de instalación. El proceso actual lee un valor estático del entorno y requiere un mecanismo externo de renovación para usar esa opción.

Comprueba `GET /admin/github/health` en preproducción después de instalar o rotar la credencial. `valida` confirma que se puede listar al menos un repositorio privado del curso. `invalida` indica HTTP 401. `acceso_privado_no_confirmado` indica que no se confirmó visibilidad privada. Verifica el permiso **Contents** con una entrega sintética de un repositorio piloto.

Genera un token aleatorio de 32 bytes por docente en el gestor de secretos y configura en `TEACHER_ADMIN_CREDENTIALS` una lista de objetos `{"id":"docente-01","section":"01","sha256":"<64 dígitos hexadecimales>"}`. Usa `"section":null` para alcance general. Distribuye el valor original al docente por el canal privado acordado. La variable del servidor contiene los hashes. Para revocar, elimina la entrada y actualiza el entorno del servicio. Restringe y retén el archivo de auditoría en el disco persistente.

El arranque del Grader desactiva el procesamiento de proxy de Uvicorn. Configura `TRUSTED_PROXY_CIDRS` con las redes del proxy que realmente entrega la conexión TCP. Restringe el acceso al backend a ese proxy. El proxy de borde debe eliminar el `X-Forwarded-For` externo y escribir o adjuntar las direcciones que observó. El Grader recorre la cadena de derecha a izquierda y se detiene en el primer salto sin confianza. Si el peer directo está fuera de las redes configuradas, el Grader usa su IP y descarta el encabezado. En el hosting de preproducción verifica una petición desde una IP conocida, otra con un encabezado falsificado y la existencia de una ruta de red restringida al backend. Los rangos dependen del proveedor y del despliegue.

## Equipos, ventanas y recibos pendientes

- `GET /admin/teams` lista el alcance de la credencial. `?section=01` lo reduce a una sección.
- `PATCH /admin/teams/{id}/assignment` recibe `{"github_repository":"lab-ia-s01-uno"}` para corregir un repositorio. Para trasladar un equipo con repositorio asignado, envía `{"section":"02","github_repository":"lab-ia-s02-uno"}` con una credencial de alcance general. El historial queda en la sección original.
- `PUT /admin/windows` recibe `{"section":"01","deliverable_id":"D2","opens_at":"2026-11-18T08:00:00-06:00","closes_at":"2026-11-18T18:00:00-06:00"}`. Para reabrir, vuelve a enviar la ventana con apertura y cierre nuevos. Antes de abrir o si no hay ventana, el recibo se marca `sin_plazo`. El cierre es inclusivo y el tiempo posterior se marca `tardío`, con minutos redondeados hacia arriba. Se usa el instante de recepción previo a consultar GitHub. Cambiar una ventana afecta los recibos que se creen posteriormente.
- Filtra el CSV por `verification_status=pendiente` y llama `POST /admin/submissions/{id}/retry-verification` por recibo. HTTP 202 indica que sigue pendiente. HTTP 200 registra `verificado` o `inconsistente`. El intento y la fecha originales permanecen en el mismo recibo.

## Copia externa programada

Instala AWS CLI en un proceso que pueda leer el volumen persistente. Usa un bucket S3 privado, con bloqueo de acceso público, retención según la política del curso y permisos de escritura limitados al prefijo de copia. Separa el permiso de lectura y restauración. El script crea una copia SQLite consistente incluso con WAL, comprueba integridad y referencias, y solicita cifrado AES256 del objeto.

Programa una copia cada hora y una copia antes de cada actualización. Ejemplo de cron en un host con el volumen montado, ajustando las rutas y el bucket:

```cron
17 * * * * cd /srv/grader && /srv/grader/.venv/bin/python -m tools.backup_db backup --db /srv/grader/data/grader.db --destination s3://BUCKET-PRIVADO/grader
```

Supervisa códigos de salida y antigüedad del último objeto. La frecuencia horaria supone hasta una hora de pérdida de recibos si falla el volumen y las copias están al día.

Para ensayar, restaura a un archivo nuevo de un entorno aislado:

```bash
python -m tools.backup_db restore --source s3://BUCKET-PRIVADO/grader/OBJETO.db --target staging/restaurado.db
python -m tools.export_section 01 --db staging/restaurado.db > staging/seccion-01.csv
```

El destino y su directorio deben estar en un filesystem que admita enlaces duros. El directorio debe existir. Para instalar la copia en el servicio, sigue el procedimiento de restauración anterior y verifica también la sección 02. La prueba automatizada usa datos sintéticos, SQLite real y una transferencia S3 simulada.

## Actualización y reversión

1. Consulta el calendario de ambas secciones y el estado de las recepciones activas. Programa la actualización fuera de las ventanas, anuncia el mantenimiento y pausa el despliegue automático si corresponde.
2. Prueba la imagen y una restauración en preproducción. En el servicio, drena las solicitudes en curso, detén las escrituras y crea una última copia externa.
3. Arranca la nueva versión sobre el volumen persistente. El arranque aplica las migraciones 001 a 006 en orden, solo si faltan. Las nuevas son `004_github_verification.sql`, `005_delivery_windows.sql` y `006_submission_team_snapshot.sql`.
4. Verifica `/health`, `tools/list`, el CSV de ambas secciones y el acceso de GitHub. Mantén una réplica activa antes de reabrir. Configura el monitor según la sección de arranque en frío.
5. Para revertir, pausa el tráfico, conserva una copia de la base posterior a la actualización, restaura la copia anterior y arranca el código anterior. Esta reversión restaura los recibos hasta la toma de esa copia. Concilia con las etiquetas oficiales cualquier recibo posterior antes de reabrir el servicio.

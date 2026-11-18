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

## Configuración de Volúmenes en Producción

SQLite guarda los equipos y las entregas en `data/grader.db`. Sin un volumen persistente, cada redespliegue que reemplace el contenedor borrará los equipos y las entregas registrados. Al arrancar, el servicio creará una base vacía.

Con el `Dockerfile` del repositorio, monta el mismo disco persistente de Render o volumen de Railway en `/app/data` en cada despliegue. Monta el directorio completo para que SQLite pueda guardar `grader.db` y sus archivos auxiliares `grader.db-wal` y `grader.db-shm` en el mismo volumen. El usuario del servicio debe poder leer y escribir en el directorio. Mantén una sola réplica del servicio con este volumen.

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

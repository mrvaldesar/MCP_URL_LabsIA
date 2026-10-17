# Skill: database_inspector

**Descripción:** Útil para verificar qué datos han sido insertados en la base de datos SQLite sin necesidad de salir del editor.

**Instrucciones para Jules:**
Cuando te pida "Revisa quiénes han enviado sus resultados" o "Verifica la base de datos", no asumas ni inventes datos. Debes ejecutar el siguiente comando en la terminal para extraer el estado real de la base de datos local:

`sqlite3 data/grader.db "SELECT * FROM submissions ORDER BY timestamp DESC LIMIT 10;"`

Si la tabla tiene un esquema diferente, primero ejecuta `.schema` para entender la estructura y luego haz la consulta. Muestra los resultados en formato de tabla Markdown.
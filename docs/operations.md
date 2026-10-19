# Operación

Referencia de comandos para el día a día de Agente U ya desplegado.
Para instalarlo desde cero, ver [deployment.md](deployment.md).

Todos los comandos asumen el despliegue estándar: código en `/opt/agente-u`,
configuración en `/etc/agente-u`, datos en `/var/lib/agente-u`, servicio
`agente-u`, usuario de servicio `agente-u`.

---

## Atajo recomendado

El scheduler se invoca con una ruta larga que vas a escribir muchas veces:

```sh
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler \
     --config /etc/agente-u/scheduler.toml --status
```

Crea un envoltorio una sola vez y olvídate:

```sh
sudo tee /usr/local/bin/agente-u >/dev/null <<'EOF'
#!/bin/sh
exec sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler \
     --config /etc/agente-u/scheduler.toml "$@"
EOF
sudo chmod 755 /usr/local/bin/agente-u
```

A partir de ahí, `agente-u --status`, `agente-u --job ingestion`, etc.
**En el resto del documento se usa la forma larga**, para que funcione aunque
no hayas creado el atajo.

---

## Estado y salud

| Qué quieres saber | Comando |
|---|---|
| ¿Está corriendo? | `sudo systemctl status agente-u --no-pager` |
| Estado del pipeline e historial | `sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler --config /etc/agente-u/scheduler.toml --status` |
| Log de hoy | `sudo journalctl -u agente-u -S today` |
| Log en vivo | `sudo journalctl -u agente-u -f` |
| Solo errores y avisos | `sudo journalctl -u agente-u -p warning -S '1 week ago'` |
| Pico de memoria desde el arranque | `sudo systemctl show agente-u -p MemoryPeak` |
| Espacio en disco | `df -h / && sudo du -sh /var/lib/agente-u/*` |

`journalctl` necesita `sudo`: el usuario de SSM no está en el grupo
`systemd-journal` y sin privilegios no ve nada, solo un aviso.

El `MemoryPeak` sale en bytes y es **acumulado desde que arrancó el
servicio**; se reinicia con `systemctl restart`. Divide entre 1048576 para
MB. Referencia medida en `t4g.small`: ~55 MB en reposo, **~981 MB** durante
un ciclo con OCR.

### Qué debe verse en un ciclo sano

```
moodle_sync attempt 1/3 done in 32.6s: 263 item(s); full: 7/7 course(s) ...
Queued ingestion (trigger=dependency); queue: ['ingestion']
ingestion attempt 1/3 done in 4.0s: 100 item(s); 100/286 attempted, ...
Queued extraction (trigger=dependency); queue: ['extraction']
extraction attempt 1/3 done in 70.3s: 50 item(s); 50/150 attempted, ...
notifications: no pending work - skipped
```

El orden siempre es el mismo y nunca hay dos jobs a la vez. El primer ciclo
tras arrancar dice `full:`; los siguientes, `incremental:`.

---

## Forzar trabajo

```sh
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler \
     --config /etc/agente-u/scheduler.toml --job NOMBRE
```

Donde `NOMBRE` es `moodle_sync`, `ingestion`, `extraction` o `notifications`.

Tres cosas que conviene entender:

- **La petición se le pasa al demonio**, no se ejecuta aparte. Si el servicio
  está corriendo, el comando solo encola y vuelve enseguida; el trabajo lo
  hace el proceso que ya tiene el lock.
- **Arrastra a los que dependen de él.** Pedir `ingestion` ejecuta después
  `extraction` y `notifications`.
- **No se duplica.** Si pides algo que ya está corriendo o en cola, se
  descarta en lugar de encolarse dos veces.

### Drenar un backlog

Al inicio de un trimestre pueden aparecer cientos de recursos de golpe, y los
topes `max_items` de `scheduler.toml` (100 por ingesta, 50 por extracción)
hacen que se procesen a lo largo de varios ciclos de 6 horas. Para acelerarlo,
repite el job hasta que `still pending` llegue a 0:

```sh
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler \
     --config /etc/agente-u/scheduler.toml --job ingestion
# ...esperar a que termine, comprobar con --status, repetir
```

La alternativa es subir temporalmente `max_items` en
`/etc/agente-u/scheduler.toml` y reiniciar. Como el presupuesto solo se
evalúa **entre** documentos, subirlo nunca corta uno a medias.

---

## Actualizar

```sh
sudo /opt/agente-u/deploy/update.sh
```

Para el servicio, avanza a la revisión publicada, reinstala solo lo que haya
cambiado, corre la suite de tests en la propia VPS y arranca de nuevo.

**Si los tests fallan deja el servicio parado** e imprime el comando de vuelta
atrás. Es deliberado: el scheduler sincroniza cada 6 horas y nunca repone
ranuras perdidas, así que estar parado un rato no cuesta nada, mientras que un ciclo
defectuoso escribe en la base de datos.

### Volver a una versión anterior

```sh
cd /opt/agente-u && git log --oneline -10
sudo systemctl stop agente-u
sudo git -C /opt/agente-u checkout <commit>
sudo systemctl start agente-u
```

Es seguro: la base de datos solo crece (filas de solo-añadir, esquema que solo
suma tablas), así que una versión anterior ignora lo que no conoce.

---

## Arrancar, parar, reiniciar

```sh
sudo systemctl stop agente-u
sudo systemctl start agente-u
sudo systemctl restart agente-u
```

Parar es seguro en cualquier momento. El apagado ordenado se atiende entre
documentos y, durante un OCR largo, entre páginas, así que tarda en torno a
una página. Al volver a arrancar, el scheduler marca como fallido cualquier
intento que quedara a medias y descarta las peticiones manuales huérfanas.

---

## Consultar los datos

Todas sobre `/var/lib/agente-u/agente_u.sqlite3`, en modo lectura. Usa
`-column -header` para que salga legible.

**Cómo se está extrayendo el material** — la proporción de PDFs que necesitan
OCR frente a los que traen texto:

```sh
sudo -u agente-u sqlite3 -column -header /var/lib/agente-u/agente_u.sqlite3 \
"SELECT extractor_name, json_extract(metadata,'\$.method') AS metodo, COUNT(*) AS n
 FROM extracted_documents WHERE status='done' GROUP BY 1,2 ORDER BY n DESC;"
```

Ojo al interpretarlo: `method` lo escriben casi todos los extractores, no solo
el de PDF. Las imágenes siempre salen como `ocr` por definición, y los enlaces
y textos sueltos no escriben `method` en absoluto. Por eso hay que agrupar
**también** por `extractor_name`.

**Qué se está quedando fuera y por qué:**

```sh
sudo -u agente-u sqlite3 -column -header /var/lib/agente-u/agente_u.sqlite3 \
"SELECT error_reason, COUNT(*) AS n FROM extracted_documents
 WHERE status='failed' GROUP BY 1 ORDER BY n DESC;"
```

**Archivos no descargados por superar el límite de tamaño** (también salen al
final de `--status`):

```sh
sudo -u agente-u sqlite3 -column -header /var/lib/agente-u/agente_u.sqlite3 \
"SELECT r.name, d.size_bytes/1048576 AS mb, d.limit_bytes/1048576 AS limite_mb, d.deferred_at
 FROM ingestion_deferrals d JOIN resources r ON r.id = d.resource_id
 ORDER BY d.size_bytes DESC;"
```

Es la consulta que sirve para **recalibrar el umbral**: muestra qué se saltó,
cuánto pesaba y qué límite estaba vigente entonces.

**Documentos que agotaron sus intentos de extracción** (también salen al final
de `--status`):

Tras tres fallos, una versión deja de ofrecerse para extracción y no se vuelve
a intentar. Sin esta lista el job seguiría informando `0 still pending` y
parecería sano, cuando en realidad hay contenido que nunca se leyó.

```
Not extracted (1 document(s) gave up after 3 attempts):
  guia-corrupta.pdf - 2.3 MB mimetype='application/pdf' course=1
      3 failure(s), last at 2026-10-13T10:22:07-04:00: PdfReadError: EOF marker not found
```

Un formato que el sistema no sabe leer **no** aparece aquí: eso es `unsupported`,
un estado distinto y benigno. Esta lista es solo para lo que se intentó leer y
no se pudo.

Para volver a intentarlo tras corregir la causa —por ejemplo si el profesor
sustituyó el archivo— basta con que Moodle cambie su `timemodified`: la
ingestión creará una versión nueva, y una versión nueva empieza con sus
intentos intactos.

**Volumen almacenado:**

```sh
sudo -u agente-u sqlite3 -column -header /var/lib/agente-u/agente_u.sqlite3 \
"SELECT COUNT(*) AS versiones, SUM(size_bytes)/1048576 AS mb FROM resource_versions;"
```

**Historial de ejecuciones reciente:**

```sh
sudo -u agente-u sqlite3 -column -header /var/lib/agente-u/agente_u.sqlite3 \
"SELECT job_name, status, started_at, duration_seconds AS seg, items_processed AS items, error
 FROM scheduler_executions ORDER BY id DESC LIMIT 20;"
```

---

## Calendarios

Cada job programado corre a **horas reales del reloj** en
`America/Santo_Domingo`, no a N horas de cuando arrancó el proceso:

| Job | Periodo | Corre a las |
|---|---|---|
| `moodle_sync` | 6 h | 00:00, 06:00, 12:00, 18:00 |
| `notifications` | 1 h (fijo) | cada hora en punto |

Si reinicias a las 17:43, el siguiente aviso no es a las 18:43: es a las
18:00. Y si la maquina estuvo apagada, **las ranuras perdidas no se reponen**
— al volver se calcula la siguiente ranura futura y ya.

`moodle_sync` arrastra consigo a `ingestion` y `extraction`, que no tienen
calendario propio: existen para procesar lo que trajo una sincronizacion.

`notifications` **no depende de esa cadena**. Decide a partir del estado ya
confirmado localmente, asi que una caida de Moodle no silencia los
recordatorios. Lo que impide avisar sobre datos sin confirmar no es el
calendario, sino la comprobacion `confirmed_since` dentro del servicio.

Al arrancar veras una linea por cada calendario:

```
INFO scheduler: moodle_sync scheduled every 6h; next run at 2026-10-07T00:00:00-04:00
INFO scheduler: notifications scheduled every 1h; next run at 2026-10-06T21:00:00-04:00
```

### Cambiar un periodo

Solo `moodle_sync` tiene periodo configurable, en `interval_hours`. **Tiene
que dividir a 24** (1, 2, 3, 4, 6, 8, 12 o 24): con cualquier otro valor la
rejilla dejaria un salto corto en medianoche, y el arranque falla diciendolo
en vez de montar un calendario que no es el que pone el archivo.

El de `notifications` **esta fijado en 1 hora y no se configura**. El ciclo y
los recordatorios no son ajustes independientes: un ciclo mas largo mataria en
silencio el aviso de la ultima hora. Fijarlo deja un suelo claro -los
recordatorios tienen que estar al menos a una hora unos de otros- y quita un
ajuste cuyo unico efecto posible era romperlos.

### Por que un run no deja rastro en el historial

Un job programado se pregunta primero si tiene algo que hacer. Si no lo
tiene, se salta **sin escribir fila** en `scheduler_executions`. Por eso
`notifications` corre 24 veces al dia pero solo aparece en el historial los
dias que envio algo. Es deliberado: mantiene legible una tabla que no se
purga nunca.

---

## Notificaciones

El canal se decide **al arrancar el proceso**, leyendo el entorno. Con
`TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` puestas, va a Telegram; sin ellas,
al log. Para saber cuál está activo ahora mismo:

```sh
sudo journalctl -u agente-u --since "$(systemctl show -p ActiveEnterTimestamp --value agente-u)" | grep -i "notifications will be\|telegram is not configured\|half configured"
```

Una de estas tres líneas aparece siempre en el arranque:

| Línea | Significa |
|---|---|
| `Notifications will be delivered to Telegram` | las dos variables están puestas |
| `Telegram is not configured: ...` | ninguna de las dos está puesta |
| `Telegram is only half configured (X is missing)` | falta una: sigue por log |

Comprobar que la entrega funciona, sin esperar a que venza una tarea:

```sh
sudo journalctl -u agente-u -S "1 week ago" | grep "Telegram notification"
```

`Telegram notification delivered: <tipo> for <sujeto>` por cada mensaje
entregado. Verás el tipo y el sujeto (`assignment:104926`), **nunca el texto**:
el contenido académico va a Telegram, no al log del sistema.

### Qué recordatorios se envían

Cada tarea pendiente genera **hasta cuatro avisos**, uno por cada regla de
`/etc/agente-u/scheduler.toml`:

| Regla | Se envía cuando quedan | Tipo almacenado |
|---|---|---|
| `24h` | entre 12 y 24 horas | `assignment_due_24h` |
| `12h` | entre 6 y 12 horas | `assignment_due_12h` |
| `6h` | entre 1 y 6 horas | `assignment_due_6h` |
| `1h` | menos de 1 hora | `assignment_due_1h` |

Cada regla se entrega **como mucho una vez por tarea y fecha de entrega**. Si
el profesor mueve la fecha, empieza una serie nueva. Si entregas, se detiene
lo que quede de la serie.

**No hay avisos retroactivos.** Una tarea que aparece por primera vez cuando
ya le quedan 5 horas solo recibe el aviso de 6 h y el de 1 h: las ventanas de
24 h y 12 h ya habían pasado y no se mandan a destiempo. Lo mismo si el
sistema estuvo apagado y se saltó una ventana.

### Cambiar los textos o las reglas

Hoy se cambian en el archivo. También pueden cambiarse en caliente desde una
interfaz (app o bot), capacidad ya construida pero sin interfaz todavía: ver
[reminder-configuration.md](reminder-configuration.md).

Las reglas son configuración, no código. En `[[notifications.reminders]]`:

- **cambiar un texto** → edita `title` o `body` y reinicia;
- **añadir un aviso** (por ejemplo a 3 h) → añade un bloque;
- **quitar uno** → bórralo, o pon `enabled = false` para conservarlo apagado;
- **mover un offset** → cambia `offset_hours`.

Las bandas se recalculan solas: si quitas el de 12 h, el de 24 h pasa a cubrir
de 6 a 24 horas. No hace falta tocar nada más.

Los marcadores disponibles son `{assignment}`, `{course}`, `{due}`,
`{due_date}`, `{due_time}`, `{remaining}` y `{tz}`. Si escribes uno que no
existe, **el arranque falla diciéndolo**, en vez de descubrirlo a mitad de un
envío.

Dos cosas que el arranque también rechaza: dos reglas con el mismo `id` —
compartirían identidad y solo llegaría una— y dos con el mismo `offset_hours`,
que haría ambigua la banda.

Cambiar solo el texto de una regla **no** reenvía avisos ya entregados: el
texto no forma parte de la identidad. Cambiar su `id` sí empieza una serie
nueva para toda tarea en curso.

### Si Telegram falla

Un fallo de entrega no detiene nada: el job de notificaciones queda en
`failed`, el scheduler sigue con su ciclo, y la notificación **no se marca
como enviada**, así que se reintenta en el siguiente ciclo.

```sh
sudo journalctl -u agente-u -p warning -S "1 day ago" | grep -i telegram
```

Los errores dicen el estado HTTP y el motivo de Telegram (`HTTP 401:
Unauthorized` = token malo; `chat not found` = `TELEGRAM_CHAT_ID` malo). El
token nunca aparece en ellos.

La semántica es **at-least-once**: si el proceso muere justo entre que
Telegram acepta el mensaje y que se registra como enviado, el recordatorio se
envía otra vez en el siguiente ciclo. Es decir, **puedes ver un duplicado**.
Es deliberado: un recordatorio repetido es inofensivo, uno perdido no.

### Desactivar Telegram

Borra las dos líneas de `/etc/agente-u/env` y reinicia. Vuelve al canal de log
sin tocar código ni base de datos.

---

## Logs

journald guarda **todo el historial**, no solo lo que ves con `-f`:

```sh
sudo journalctl -u agente-u --since "2026-10-05 00:00" --until "2026-10-05 06:00"
sudo journalctl -u agente-u -p warning -S "1 week ago"
sudo journalctl -u agente-u --no-pager > ~/agente-u-$(date +%F).log
sudo journalctl --disk-usage
```

Está limitado a 200 MB (`/etc/systemd/journald.conf.d/size.conf`); al llenarse
descarta lo más antiguo.

**Comprueba que sobreviva a los reinicios.** journald solo es persistente si
existe `/var/log/journal`; si no, escribe en RAM y se pierde al reiniciar:

```sh
ls -d /var/log/journal 2>/dev/null && echo "PERSISTENTE" || echo "VOLATIL"
```

Si sale volátil, se arregla una vez:

```sh
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo systemctl restart systemd-journald
```

### El segundo registro, más duradero

La tabla `scheduler_executions` guarda cada intento de cada job con su
duración, ítems y error. Frente a journald: **no rota nunca** y **entra en la
copia de seguridad diaria**. Es append-only por diseño (DEC-038).

| | journald | `scheduler_executions` |
|---|---|---|
| Detalle | todo, línea a línea | un registro por intento |
| Rotación | sí, a los 200 MB | nunca |
| Copia de seguridad | no | sí, diaria |
| Para qué sirve | diagnóstico reciente | auditoría histórica |

---

## Cerrar un trimestre

Agente U **nunca borra datos por su cuenta**: todo lo que sincroniza se queda
hasta que tú lo quites. Al acabar un trimestre, el material de asignaturas que
ya no cursas sigue ocupando disco y, sobre todo, sigue siendo información
personal almacenada sin necesitarla.

El cierre tiene dos partes, y **el orden importa**.

### 1. Archiva lo que quieras conservar

El purgador **no archiva nada**. Los originales se guardan direccionados por
contenido (`<sha256>` como nombre), así que una copia útil tiene que
reconstruir los nombres y la estructura del curso — y eso se hace donde vaya a
vivir el archivo, no en la máquina que se está vaciando.

Si quieres conservarlo, hazlo **antes** de purgar.

### 2. Purga

```sh
sudo systemctl stop agente-u
```

Primero mira qué se borraría. Sin `--yes` no toca nada:

```sh
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler     --config /etc/agente-u/scheduler.toml --purge
```

Y cuando estés conforme:

```sh
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler     --config /etc/agente-u/scheduler.toml --purge --yes
```

```sh
sudo systemctl start agente-u
```

El primer ciclo tras arrancar es una sincronización completa, que reconstruye
el trimestre nuevo desde Moodle.

### Qué borra y qué no

| Se borra | Se conserva |
|---|---|
| Cursos, secciones, módulos, archivos | Historial de ejecuciones (`scheduler_executions`) |
| Tareas, estados de entrega, notas | Tus reglas de recordatorio |
| Eventos de calendario | Registro de avisos ya enviados |
| Recursos, versiones y extracciones | |
| Perfil de usuario de Moodle | |
| **Todos los originales descargados** | |

Se conserva el registro de avisos enviados a propósito: borrarlo haría que un
purgado a destiempo te reanunciara entregas que sobreviven a la purga.

### Las tres guardas

El comando se niega a actuar si **el scheduler está corriendo** —purgar bajo un
proceso que escribe dejaría descargas huérfanas—, exige `--yes` de forma
explícita, y **escribe un backup antes de borrar nada**. Si el backup falla, no
borra.

El backup queda junto a la base, con marca de tiempo:

```sh
sudo ls -lh /var/lib/agente-u/agente_u-before-purge-*.sqlite3
```

Para deshacer una purga, ese archivo es la base de antes: párala, cópiala
encima y arranca. Usa el **nombre literal** que imprimió la purga, no un
comodín (ver "Cuidado con los comodines" más abajo).

Los **originales descargados no están en ese backup** — se vuelven a bajar de
Moodle en el siguiente ciclo, mientras el curso siga existiendo. Si quieres
poder volver al estado exacto, guárdalos aparte **antes** de purgar.

### Probado de punta a punta

Ensayado contra producción el **2026-10-13**, con el trimestre en curso:
purga completa, verificación de que no quedó nada, restauración desde el
backup que la propia purga escribió, y comprobación de que los recuentos
volvían a coincidir.

```
antes:      2155 filas, 260 ficheros (121.4 MB)
tras purgar:   0 filas,   0 ficheros
restaurado: 2155 filas, 260 ficheros
```

El ensayo en seco (`--purge` sin `--yes`) se puede correr **con el servicio
encendido**: solo cuenta filas. La purga en sí exige pararlo.

---

## Copias de seguridad

Se hacen solas cada día a las 04:30 mediante `agente-u-backup.timer`.

```sh
sudo systemctl list-timers agente-u-backup --no-pager   # cuándo toca la próxima
sudo systemctl start agente-u-backup.service            # forzar una ahora
sudo journalctl -u agente-u-backup -S '1 week ago'      # cómo fueron las últimas
sudo ls -lh /var/backups/agente-u/                      # qué hay guardado
```

Se conservan las 14 más recientes, comprimidas, y cada una se verifica con
`PRAGMA integrity_check` antes de darse por buena.

**Los originales descargados no se respaldan**: son copias direccionadas por
contenido de archivos que Moodle sigue teniendo, y recuperarlos cuesta una
sincronización. La base de datos es lo único irrecuperable.

### Restaurar

**Verificado en producción el 2026-10-13**: purga completa y restauración, con
los recuentos coincidiendo exactamente antes y después.

Primero, ver qué hay. Fíjate en el `sudo` delante del `ls`:

```sh
sudo ls -lht /var/backups/agente-u/
```

Luego restaurar, **con el nombre literal del archivo**:

```sh
sudo systemctl stop agente-u
sudo -u agente-u sh -c 'gunzip -c /var/backups/agente-u/agente_u-<fecha>.sqlite3.gz \
    > /var/lib/agente-u/agente_u.sqlite3'
```

Comprobar qué tiene la base restaurada, antes de arrancar:

```sh
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler \
    --config /etc/agente-u/scheduler.toml --purge
```

Ese comando **no borra nada sin `--yes`**; aquí sirve de recuento. Compara el
total con el que esperabas de la copia.

```sh
sudo systemctl start agente-u
```

### Cuidado con los comodines

`/var/lib/agente-u` está en modo 700 y pertenece a `agente-u`, así que **tu
shell no puede expandir un `*` ahí dentro** — ni siquiera anteponiendo `sudo`,
porque la expansión ocurre antes de que `sudo` arranque. Esto falla:

```sh
sudo -u agente-u cp /var/lib/agente-u/agente_u-before-purge-*.sqlite3 ...
# cp: cannot stat '...-*.sqlite3': No such file or directory
```

Usa el nombre completo, o deja que la expansión la haga root:

```sh
sudo sh -c 'ls -lh /var/lib/agente-u/agente_u-before-purge-*.sqlite3'
```

### Los originales no están en el backup

La copia contiene **solo la base de datos**. Los ~120 MB de originales
descargados no, y eso tiene dos consecuencias:

- Tras restaurar, el siguiente ciclo **vuelve a descargar** lo que falte de
  Moodle. Funciona mientras el curso siga existiendo.
- Si necesitas el estado exacto —por ejemplo para ensayar una restauración—
  guárdalos aparte antes:

```sh
sudo tar czf /var/tmp/originals.tar.gz -C /var/lib/agente-u originals
sudo tar xzf /var/tmp/originals.tar.gz -C /var/lib/agente-u
sudo chown -R agente-u:agente-u /var/lib/agente-u/originals
```

Ese `chown` no es opcional: `tar` ejecutado como root restaura la propiedad
guardada en el archivo, y si los ficheros quedan como `root` el servicio no
puede leer sus propios datos.

El número de ficheros **no coincide** con el de `resource_versions`: el almacén
está direccionado por contenido, así que dos recursos con contenido idéntico
comparten uno solo. En la prueba del 13 de octubre: 290 versiones, 260
ficheros.

---

## Verificar contra Moodle real

La suite normal no toca la red: los 11 tests de integración se saltan salvo
que les des credenciales. Es deliberado — CI no debe llamar a tu universidad
ni guardar tu token —, pero significa que **el camino real contra Moodle solo
se ejercita cuando lo pides**.

Son de **solo lectura** contra Moodle, y cada uno escribe en un directorio
temporal: nunca tocan la base de producción ni el repositorio.

```sh
MOODLE_RUN_INTEGRATION_TESTS=1 MOODLE_URL=https://campusvirtual.example.edu MOODLE_TOKEN=<tu-token>     python -m unittest discover -s tests/integration -t .
```

En PowerShell:

```powershell
$env:MOODLE_RUN_INTEGRATION_TESTS=1
$env:MOODLE_URL="https://campusvirtual.example.edu"
$env:MOODLE_TOKEN="<tu-token>"
python -m unittest discover -s tests/integration -t .
```

Qué cubren, y por qué conviene correrlos tras tocar el cliente de Moodle, la
ingestión o la extracción:

| Test | Qué demuestra |
|---|---|
| `test_moodle_client_smoke` | autenticación, listado de cursos y **una descarga real** |
| `test_moodle_sync_smoke` | perfil, cursos, tareas, estado de entrega, notas, calendario |
| `test_ingestion_smoke` | descubrimiento, descarga, hash y versiones, idempotente |
| `test_read_model_smoke` | las consultas del Read Model contra datos reales |
| `test_scheduler_smoke` | un ciclo de arranque y uno incremental, completos |

Los de OCR van aparte, con `RUN_OCR_INTEGRATION_TESTS=1`: descargan los pesos
del modelo y tardan bastante, así que no los mezcles con lo anterior salvo que
estés tocando OCR.

### Cuándo importa de verdad

Un doble de test no puede decirte si **Moodle responde como esperas**. Por
ejemplo, la comprobación de descargas truncadas compara el `Content-Length`
declarado con los bytes recibidos: si tu Moodle no enviara esa cabecera, o la
enviara de otra forma, solo lo descubrirías aquí.

Última ejecución completa: **2026-10-12**, 7 tests, todos OK — 0 fallos de
descarga sobre 3 ficheros reales en el ciclo del scheduler.

---

## Diagnóstico

| Síntoma | Qué mirar |
|---|---|
| El servicio no arranca | `sudo journalctl -u agente-u -n 50`. Lo más común: `scheduler.toml` inválido (sale `Configuration error:` y salida 2) o falta el archivo de entorno |
| `moodle_sync` falla siempre | ¿Token caducado o revocado? El error dirá `invalidtoken`. Se cambia en `/etc/agente-u/env` y se reinicia |
| Todo falla tras un reinicio de red | Comprueba salida HTTPS: `curl -sI https://campusvirtual.example.edu \| head -1` |
| Extracciones fallando en masa | `sudo -u agente-u ... --status` y la consulta de `error_reason`. Si dice `unsupported format`, es formato fuera del MVP, no una avería |
| El proceso muere sin mensaje | `sudo dmesg \| tail` — si hay `Out of memory`, fue el OOM killer |
| Disco lleno | `df -h /`. La aplicación reserva 256 MB y rechaza almacenar por debajo de eso con un error explícito |
| `database is locked` | Hay otro proceso con el lock. `--status` dice si el demonio está corriendo |
| El log se llena de avisos de `pypdf._cmap` | Falta `fontTools`. Se resuelve con la extra `pypdf[fonts]`, ya declarada en `pyproject.toml` |
| `DependencyError` al extraer un PDF | PDF cifrado: pypdf necesitaría su extra `crypto`. Fuera del MVP; queda registrado como fallo |
| Segunda instancia rechazada | Es correcto: solo puede haber un scheduler. Sale con código 2 |

### Comprobar el entorno sin exponer el token

```sh
sudo awk -F= '/^MOODLE_URL=/{print "URL:", $2} \
              /^MOODLE_TOKEN=/{print "moodle token: longitud", length($2)} \
              /^TELEGRAM_BOT_TOKEN=/{print "telegram token: longitud", length($2)} \
              /^TELEGRAM_CHAT_ID=/{print "telegram chat id: definido"}' \
     /etc/agente-u/env
sudo grep -c $'\r' /etc/agente-u/env    # debe dar 0: los CR rompen el parseo de systemd
```

Nunca imprimas el archivo entero: `MOODLE_TOKEN` es la credencial completa de
tu Moodle, y `TELEGRAM_BOT_TOKEN` el control total del bot.

---

## Rutas

| Ruta | Qué es | Permisos |
|---|---|---|
| `/opt/agente-u` | código y virtualenv | root, solo lectura para el servicio |
| `/etc/agente-u/scheduler.toml` | configuración, sin secretos | 644 |
| `/etc/agente-u/env` | `MOODLE_URL` y `MOODLE_TOKEN` | **600, root** |
| `/var/lib/agente-u/agente_u.sqlite3` | base de datos académica | 600, `agente-u` |
| `/var/lib/agente-u/originals/` | archivos originales descargados | 700, `agente-u` |
| `/var/lib/agente-u/doctr-cache/` | modelos de OCR (~129 MB) | `agente-u` |
| `/var/backups/agente-u/` | copias de la base de datos | 700, `agente-u` |
| `/etc/systemd/system/agente-u*.service` | unidades systemd | 644 |

---

## Códigos de salida

| Código | Significado |
|---|---|
| 0 | correcto |
| 1 | error de ejecución |
| 2 | no disponible: configuración inválida, o ya hay otra instancia corriendo |
| 130 | interrumpido con `Ctrl+C` |

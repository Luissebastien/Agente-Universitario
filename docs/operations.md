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
atrás. Es deliberado: el scheduler corre cada 6 horas y nunca repone ciclos
perdidos, así que estar parado un rato no cuesta nada, mientras que un ciclo
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

```sh
sudo systemctl stop agente-u
sudo -u agente-u sh -c 'gunzip -c /var/backups/agente-u/agente_u-<fecha>.sqlite3.gz \
    > /var/lib/agente-u/agente_u.sqlite3'
sudo systemctl start agente-u
```

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
sudo awk -F= '/^MOODLE_URL=/{print "URL:", $2} /^MOODLE_TOKEN=/{print "token: longitud", length($2)}' \
     /etc/agente-u/env
sudo grep -c $'\r' /etc/agente-u/env    # debe dar 0: los CR rompen el parseo de systemd
```

Nunca imprimas el archivo entero: `MOODLE_TOKEN` es la credencial completa de
tu Moodle.

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

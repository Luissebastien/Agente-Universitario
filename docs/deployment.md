# Despliegue en VPS

Guía para ejecutar Agente U de forma permanente en un servidor Linux ARM64.
Verificada contra **Oracle Cloud Always Free, Ampere A1 (2 OCPU / 12 GB RAM)**
con **Ubuntu 24.04 ARM64**, que es el entorno para el que se eligieron todos
los valores de esta guía.

El proceso no abre ningún puerto de entrada. Solo necesita salida HTTPS.

---

## 1. Requisitos

| | Valor | Por qué |
|---|---|---|
| Arquitectura | ARM64 (aarch64) o x86_64 | Todas las dependencias nativas tienen wheel para ambas |
| RAM | ≥ 2 GB; 12 GB recomendado | Con OCR el pico medido por documento es ~1.3 GB. Una VM de 1 GB **no** soporta OCR |
| Disco | ~5 GB | venv ~1.5 GB, modelos OCR ~250 MB, originales y base de datos el resto |
| **Python** | **3.12 o superior** | `numpy` y `scipy` declaran `requires_python >= 3.12`. En 3.11 pip resolvería versiones más antiguas, es decir, una combinación nunca probada |
| Salida de red | HTTPS (443) a dos hosts | Ver §5 |

Ubuntu 24.04 trae Python 3.12 por defecto, así que no hace falta añadir repositorios.

### Paquetes del sistema

```sh
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip sqlite3 git \
                    libgl1 libglib2.0-0
```

`libgl1` y `libglib2.0-0` no son opcionales: docTR exige `opencv-python`
(no la variante *headless*), que enlaza `libGL.so.1`. Sin ellos `import cv2`
falla y con él todo el OCR.

---

## 2. Usuario y directorios

```sh
sudo useradd --system --home /var/lib/agente-u --shell /usr/sbin/nologin agente-u
sudo mkdir -p /opt/agente-u /etc/agente-u /var/backups/agente-u
sudo chown root:root /opt/agente-u /etc/agente-u
sudo chown agente-u:agente-u /var/backups/agente-u
sudo chmod 700 /var/backups/agente-u
```

| Ruta | Contenido | Acceso del servicio |
|---|---|---|
| `/opt/agente-u` | código y virtualenv | solo lectura |
| `/etc/agente-u` | `scheduler.toml` y `env` | solo lectura |
| `/var/lib/agente-u` | base de datos, originales, caché de modelos | lectura y escritura, modo 0700 |
| `/var/backups/agente-u` | copias de la base de datos | lectura y escritura, modo 0700 |

`/var/lib/agente-u` lo crea y lo posee systemd mediante `StateDirectory`, con
modo 0700 y `UMask=0077`. Esa es la única protección de los datos académicos
en reposo: la aplicación no fija permisos por su cuenta.

---

## 3. Instalación

```sh
sudo git clone https://github.com/Luissebastien/Agente-Universitario.git /opt/agente-u
cd /opt/agente-u
sudo python3.12 -m venv .venv
sudo .venv/bin/python -m pip install --upgrade 'pip>=26.2'   # ver §9
sudo .venv/bin/python -m pip install -c constraints.txt -e .
```

`constraints.txt` fija el conjunto exacto de versiones con el que pasa la
suite de pruebas. Sin él, `pip install -e .` resolvería lo más nuevo que
exista ese día, que no es necesariamente lo que se validó.

Comprobación inmediata de que no falta ninguna biblioteca del sistema:

```sh
sudo .venv/bin/python -c "import torch, cv2, numpy, scipy, doctr, pypdfium2, lxml; print('ok')"
```

Si falla en `cv2`, falta `libgl1` (§1).

---

## 4. Configuración y secretos

```sh
sudo install -m 644 deploy/scheduler.toml /etc/agente-u/scheduler.toml

sudo tee /etc/agente-u/env >/dev/null <<'EOF'
MOODLE_URL=https://campusvirtual.example.edu
MOODLE_TOKEN=<pega-aqui-el-token>
EOF
sudo chmod 600 /etc/agente-u/env
sudo chown root:root /etc/agente-u/env
```

El token **solo** vive en este archivo. Nunca en `scheduler.toml`, nunca en el
repositorio, nunca en una variable de entorno de tu shell interactivo (queda
en el historial). `scheduler.toml` no admite secretos por diseño.

El archivo tiene que estar en modo 600: lo lee systemd como root antes de
bajar a `agente-u`, de modo que el propio servicio no necesita leerlo.

---

## 5. Salida de red

Solo dos destinos, ambos HTTPS:

| Host | Para qué | Cuándo |
|---|---|---|
| `campusvirtual.example.edu` | sincronización y descarga de archivos | cada ciclo |
| `doctr-static.mindee.com` | pesos de los modelos OCR | solo la primera vez que se usa OCR |

Si restringes la salida, **bloquea el puerto 80**. La librería docTR, si falla
la descarga por HTTPS, reintenta automáticamente por HTTP en claro
(`doctr/utils/data.py`). Verifica el SHA-256 de lo descargado, pero es mejor
no darle la oportunidad.

### Precargar los modelos (recomendado)

Así el primer ciclo real no depende de la red hacia un tercero:

```sh
sudo -u agente-u DOCTR_CACHE_DIR=/var/lib/agente-u/doctr-cache \
    /opt/agente-u/.venv/bin/python -c \
    "from doctr.models import ocr_predictor; ocr_predictor(pretrained=True); print('modelos listos')"
```

---

## 6. Servicio

```sh
sudo install -m 644 deploy/agente-u.service        /etc/systemd/system/
sudo install -m 644 deploy/agente-u-backup.service /etc/systemd/system/
sudo install -m 644 deploy/agente-u-backup.timer   /etc/systemd/system/
sudo chmod 755 /opt/agente-u/deploy/backup.sh

sudo systemctl daemon-reload
sudo systemctl enable --now agente-u.service
sudo systemctl enable --now agente-u-backup.timer
```

systemd solo arranca, supervisa y reinicia. **No decide cuándo corre nada:**
el intervalo de 6 horas, la sincronización completa de arranque, la cadena de
dependencias y los reintentos viven dentro del proceso, como exige
`.ai/SCHEDULER-MVP-RULES.md`. Por eso no hay un *timer* para el scheduler.

---

## 7. Verificación

```sh
systemctl status agente-u --no-pager
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler \
     --config /etc/agente-u/scheduler.toml --status
journalctl -u agente-u -f
```

Qué debe pasar:

1. **Arranque** → una sincronización *completa* (`detail: full: ...`), no incremental.
2. **Cadena** → `moodle_sync` → `ingestion` → `extraction` → `notifications`, en ese orden y de a una.
3. **Segundo ciclo** (a las 6 h) → `detail: incremental: ...`.
4. **Reinicio de la VM** → arranca solo y vuelve a hacer una sincronización completa. **No** repone los ciclos perdidos; eso es intencional.
5. **Sin token en los logs** → `journalctl -u agente-u | grep -c '<primeros-8-del-token>'` debe dar `0`.
6. **Segunda instancia rechazada** → ejecutar el scheduler a mano mientras el servicio corre debe salir con código 2.

Y la suite completa, que valida la portabilidad de todo el código propio:

```sh
cd /opt/agente-u && sudo -u agente-u .venv/bin/python -m unittest discover -s tests -t .
```

Esperado: **416 tests, 0 fallos, 11 omitidos** (los omitidos son los de
integración, que requieren credenciales).

---

## 8. Operación

```sh
# Estado, historial y archivos aplazados
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler \
     --config /etc/agente-u/scheduler.toml --status

# Forzar un job ahora (se encola si el scheduler está ocupado)
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler \
     --config /etc/agente-u/scheduler.toml --job moodle_sync

# Logs
journalctl -u agente-u -S today
journalctl -u agente-u-backup -S '1 week ago'
```

### Reajustar el límite de descarga

Un archivo por encima del límite (64 MB) no se descarga: se registra y se
deja a la espera de una decisión, **sin reintentarlo cada ciclo**. `--status`
los lista de mayor a menor, con el tamaño que se vio y el límite que estaba
vigente:

```
Not ingested (1 resource(s) waiting for a decision):
  [oversized] video-clase-3.mp4 - 295.6 MB (limit in force: 64 MB) course=101 since ...
```

Si decides subir el límite, cambia `MAX_RESOURCE_BYTES` en
`src/moodle/client.py`, reinicia el servicio y el archivo se reevalúa en el
siguiente ciclo (si el profesor lo reemplaza, también se reevalúa solo).

### Restaurar una copia

```sh
sudo systemctl stop agente-u
sudo -u agente-u sh -c 'gunzip -c /var/backups/agente-u/agente_u-<fecha>.sqlite3.gz \
    > /var/lib/agente-u/agente_u.sqlite3'
sudo systemctl start agente-u
```

Los **originales descargados no se respaldan**: son copias direccionadas por
contenido de archivos que Moodle sigue teniendo, y recuperarlos cuesta una
sincronización. La base de datos es lo único que no se puede reconstruir.

---

## 9. Mantenimiento

| Tarea | Frecuencia | Nota |
|---|---|---|
| `pip install --upgrade 'pip>=26.2'` | al instalar | `pip` 26.0.1 tiene 4 advisories MODERATE (CVE-2026-3219, -6357, -8643, -13346) |
| Revisar archivos aplazados | mensual | `--status`; sirve para calibrar el límite |
| Revisar espacio en disco | mensual | el backup avisa por debajo de 1 GB |
| Actualizar dependencias | deliberadamente | regenerar `constraints.txt` y volver a correr la suite |

---

## 10. Límites y decisiones vigentes

Valores derivados del corpus real de la instancia real (199 archivos: mediana 0.09 MB,
p99 11.42 MB, mayor 34.24 MB; documento más pesado un libro de 786 páginas de
5.93 MB):

| Límite | Valor | Dónde |
|---|---|---|
| Tamaño de una descarga | 64 MB | `src/moodle/client.py` |
| Píxeles de una imagen | 50 MP | `src/extraction/ocr.py` |
| Píxeles de una página renderizada | 50 MP (baja el dpi, no falla) | `src/extraction/extractors.py` |
| Miembro de un zip de Office | 50 MB, 200 MB por documento, ratio 100:1 | `src/extraction/ocr.py` |
| Imágenes incrustadas por documento | 20 | `src/extraction/ocr.py` |
| Reserva de disco | 256 MB | `src/ingestion/storage.py` |
| Intentos por job | 3 | `src/scheduler/scheduler.py` |
| Memoria del proceso | 4 GB (tope de systemd) | `deploy/agente-u.service` |

**No hay límite de páginas ni de tiempo por documento**, a propósito: un libro
escaneado es material legítimo y debe poder terminar. Un apagado ordenado se
atiende entre páginas, así que detener el servicio toma aproximadamente una
página, no un documento.

Riesgo asumido: no hay punto de control *dentro* de un documento. Si el OCR de
un documento tardase más que el tiempo entre reinicios del proceso, no llegaría
a completarse nunca. Para un libro de 786 páginas serían ~1.1 h estimadas, muy
por debajo del tiempo entre reinicios habituales.

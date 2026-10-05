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
sudo apt install -y python3 python3-venv python3-pip sqlite3 git \
                    libgl1 libglib2.0-0
```

En Ubuntu 26.04 el metapaquete `python3-venv` puede no arrastrar `ensurepip`.
Si `python3 -m venv` falla con *"ensurepip is not available"*, instala el de la
versión concreta (`python3.14-venv` en 26.04). Ojo: comprobar `python3 -m venv
--help` **no** detecta este caso; `python3 -c "import ensurepip"` sí.

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
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install --upgrade 'pip>=26.2'   # ver §9

# 1) torch y torchvision desde el índice CPU de PyTorch - ver aviso abajo
sudo env TMPDIR=/var/tmp .venv/bin/python -m pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    "torch==2.14.1" "torchvision==0.29.1"

# 2) el resto, desde PyPI con las versiones fijadas
sudo env TMPDIR=/var/tmp .venv/bin/python -m pip install --no-cache-dir \
    -c constraints.txt -e .
```

**Los dos pasos no son opcionales en Linux.** El wheel de `torch` publicado en
PyPI para Linux declara dependencias de CUDA (`nvidia-cudnn` son 651 MB por sí
solo, y el conjunto pasa de 4 GB) aunque la máquina no tenga GPU. En Windows el
wheel por defecto ya es CPU-only, así que el problema solo aparece al desplegar.
Instalando torch primero desde el índice CPU, el segundo paso ve
`torch<3.0.0,>=2.0.0` ya satisfecho y nunca lo busca en PyPI.

`torch==2.14.1` de `constraints.txt` acepta `2.14.1+cpu`: según PEP 440, un
especificador sin etiqueta local ignora la etiqueta local del candidato.

`TMPDIR=/var/tmp` hace falta porque en Ubuntu moderno `/tmp` es un tmpfs
respaldado por RAM (918 MB en una máquina de 2 GiB) y la descarga no cabe.
`sudo` no propaga variables de entorno, de ahí el `env`.

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

Esperado: **426 tests, 0 fallos, 11 omitidos** (los omitidos son los de
integración, que requieren credenciales).

---

## 8. Operación

Los comandos del día a día — estado, logs, forzar un job, consultar la base
de datos, copias, diagnóstico — están en **[operations.md](operations.md)**.

Lo mínimo para comprobar que sigue vivo:

```sh
sudo systemctl status agente-u --no-pager
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler      --config /etc/agente-u/scheduler.toml --status
sudo journalctl -u agente-u -S today
```

---

## 9. Actualizar

Un solo comando. Para el servicio, avanza a la revisión publicada, reinstala
solo lo que cambió, corre los 426 tests en la propia VPS y vuelve a arrancar:

```sh
sudo /opt/agente-u/deploy/update.sh
```

Detecta por sí mismo qué hace falta según los archivos que cambiaron:
dependencias si se tocó `pyproject.toml` o `constraints.txt`, la
configuración si cambió `deploy/scheduler.toml`, y las unidades si cambió
alguna de `deploy/*.service` o `.timer`.

**Si los tests fallan, deja el servicio parado a propósito** e imprime el
comando exacto de vuelta atrás. Arrancar código que falla sus propias pruebas
sería peor que un scheduler inactivo un rato: corre cada 6 horas y nunca
repone ciclos perdidos, así que estar parado no cuesta nada, mientras que un
ciclo defectuoso escribe en la base de datos.

Código Python a secas no necesita reinstalar nada, porque la instalación es
editable (`pip install -e .`): el venv apunta a `/opt/agente-u/src`, así que
el `git merge` ya deja el código nuevo en su sitio y basta con reiniciar.

### El hueco que debes conocer

Una **tabla nueva** en `db.py` aparece sola, porque el esquema usa
`CREATE TABLE IF NOT EXISTS` y se ejecuta en cada conexión. Una **columna
nueva en una tabla existente, no**: el proyecto no tiene mecanismo de
migraciones. Por eso los aplazamientos de ingesta se guardan en una tabla
propia (`ingestion_deferrals`) en lugar de añadir columnas a
`ingestion_source_checks`.

### Volver atrás

```sh
cd /opt/agente-u && git log --oneline -5
sudo systemctl stop agente-u && sudo git checkout <commit> && sudo systemctl start agente-u
```

Revertir código es seguro porque la base de datos solo crece: las filas son
de solo-añadir y el esquema solo suma tablas, así que una versión anterior
ignora lo que no conoce en lugar de romperse.

---

## 10. Mantenimiento

| Tarea | Frecuencia | Nota |
|---|---|---|
| `pip install --upgrade 'pip>=26.2'` | al instalar | `pip` 26.0.1 tiene 4 advisories MODERATE (CVE-2026-3219, -6357, -8643, -13346) |
| Revisar archivos aplazados | mensual | `--status`; sirve para calibrar el límite |
| Revisar espacio en disco | mensual | el backup avisa por debajo de 1 GB |
| Actualizar dependencias | deliberadamente | regenerar `constraints.txt` y volver a correr la suite |

---

## 11. Límites y decisiones vigentes

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
| Memoria del proceso | 1.5 GB (tope de systemd, para 2 GiB de RAM) | `deploy/agente-u.service` |

**No hay límite de páginas ni de tiempo por documento**, a propósito: un libro
escaneado es material legítimo y debe poder terminar. Un apagado ordenado se
atiende entre páginas, así que detener el servicio toma aproximadamente una
página, no un documento.

Riesgo asumido: no hay punto de control *dentro* de un documento. Si el OCR de
un documento tardase más que el tiempo entre reinicios del proceso, no llegaría
a completarse nunca. Para un libro de 786 páginas serían ~1.1 h estimadas, muy
por debajo del tiempo entre reinicios habituales.

# Configurar recordatorios desde una interfaz

Las reglas de recordatorio se pueden cambiar **en caliente**, sin reiniciar el
servicio y sin tocar ningún archivo del sistema. Esta capacidad está
construida y probada, pero **todavía no hay ninguna interfaz que la use**: hoy
el sistema corre siempre con las reglas de `scheduler.toml`.

Este documento existe para que quien construya esa interfaz —un comando de
Telegram, una app de iOS/Android, una página web— sepa exactamente contra qué
programar.

## Por qué no se edita el TOML

Dos barreras del diseño actual lo impiden, y ambas son deseables:

1. **La configuración se lee una sola vez, al arrancar.** Un cambio en el TOML
   no surte efecto sin reiniciar el proceso.
2. **El servicio no puede escribir en `/etc`.** `ProtectSystem=strict` deja
   todo el sistema de archivos en solo lectura salvo `/var/lib/agente-u`. Es
   endurecimiento deliberado: una vulnerabilidad en una interfaz no se
   convierte en control del host.

Por eso las preferencias viven en SQLite, que sí es territorio del servicio.

## El reparto: quién manda sobre qué

| Ajuste | Dónde | Quién lo cambia | Efecto |
|---|---|---|---|
| Rutas, presupuestos, límites de recursos | `scheduler.toml` | operador, con acceso al host | requiere reinicio |
| Qué recordatorios quieres y cómo se leen | SQLite | el estudiante, desde cualquier interfaz | siguiente ciclo |

El criterio no es la comodidad, es **de quién es el ajuste**. Un estudiante no
debería poder cambiar desde un chat cuánta CPU consume el proceso.

## La API

Todo pasa por [`src/database/reminder_repository.py`](../src/database/reminder_repository.py).
Ninguna interfaz aparece ahí, y ninguna necesita aparecer.

```python
from database import reminder_repository as repo
from notifications.reminders import ReminderRule, ReminderError

# Lo que está vigente ahora mismo
rules = repo.effective_rules(conn, config.notifications.reminders)

# ¿Ha elegido el estudiante alguna vez, o corren los valores del host?
repo.has_saved(conn)          # bool
repo.get_rules(conn)          # None = nunca eligió; () = pidió silencio total

# Guardar un juego completo de reglas
repo.replace_rules(conn, nuevas_reglas, disabled=reglas_apagadas)

# Volver a los valores del host
repo.clear_rules(conn)
```

### Tres estados, no dos

Esta distinción es la que más fácil se rompe al implementar una interfaz:

| Estado | `get_rules()` | Significa |
|---|---|---|
| Nunca eligió | `None` | aplican las reglas de `scheduler.toml` |
| Eligió reglas | tupla con contenido | aplican las suyas |
| Pidió silencio | `()` | **no se envía nada**, y no se vuelve a los valores por defecto |

"Apágamelo todo" y "nunca lo configuré" **no son lo mismo**. Por eso el hecho
de haber elegido se guarda aparte de la elección, en
`reminder_preferences_saved`.

### Reglas apagadas sin perder el texto

`disabled=` conserva reglas que el estudiante desactivó, con su texto intacto,
para que volver a encenderlas no obligue a reescribirlas. No se entregan y no
las devuelve `get_rules()`.

## La invariante que protege todo

`replace_rules()` llama a `notifications.reminders.validate()` antes de
escribir nada. Si falla, lanza `ReminderError` y **la base queda como estaba**.

Rechaza tres cosas:

- **dos reglas con el mismo `id`** — compartirían identidad de deduplicación y
  solo llegaría una;
- **dos con el mismo offset** — la banda sería ambigua;
- **dos reglas a menos de una hora de distancia** — y esta es la importante.

### La regla de la hora

Los recordatorios se evalúan **una vez por hora, siempre**. No es configurable,
a propósito: el ciclo y los recordatorios no son ajustes independientes.

Un ciclo alineado al reloj de periodo P cae **exactamente una vez** dentro de
cualquier ventana de ancho ≥ P, y puede **saltarse limpiamente** una más
estrecha. Un recordatorio de 30 minutos no es un aviso que llega tarde: es uno
que **no llega nunca, en silencio**.

De ahí el suelo: **dos recordatorios no pueden estar a menos de una hora**, y
el último no puede estar a menos de una hora de la entrega.

Ojo, porque no es el offset más pequeño lo que manda, sino el **hueco** más
pequeño: avisos a 24 h y a 23 h 30 están ambos lejos de la entrega, pero el
hueco entre ellos mide 30 minutos y se rechaza igual.

El mensaje de error dice qué se permite:

```
the tightest gap between reminders is 30 minutes, but reminders are checked
once an hour, so that one would fall between two runs and never be sent.
Reminders must be at least 1 hour apart, and the earliest one at least that
far before the deadline.
```

Una interfaz debe **mostrar ese error al usuario**, no tragárselo. Es la
diferencia entre "no puedo guardarte eso y aquí está el motivo" y un
recordatorio que el usuario cree tener y no tiene.

**Nota de diseño:** por el camino pasamos por dos ideas peores. Primero,
*derivar* el ciclo de la regla más estrecha: dejaría que un usuario bajara la
frecuencia a un minuto añadiendo un recordatorio de un minuto, y eso es una
decisión de recursos del host. Después, dejar el ciclo configurable por el
operador: añade un ajuste cuyo único efecto posible es romper recordatorios.
Fijarlo en una hora elimina la clase entera de problema. Esto sustituye la nota
post-MVP de DEC-068.

## Qué falta para tener la interfaz

Nada de esto está construido:

- **recepción de mensajes** (Telegram Fase 2: long polling, o un endpoint para
  una app);
- **autorización** — solo el `chat_id` configurado, o la sesión autenticada de
  la app, puede escribir;
- **una superficie de comandos** o pantallas;
Lo que **sí** está cubierto, y la interfaz no tiene que reimplementar: las
plantillas se comprueban al guardar. Un `{materia}` mal escrito o un `{` sin
cerrar se rechaza con `ReminderError` antes de escribir nada, en vez de
reventar a mitad de un envío con parte del lote ya entregado. Los marcadores
válidos están en `TEMPLATE_FIELDS`, y el error los enumera.

## Comprobar qué hay vigente

```sh
sudo -u agente-u /opt/agente-u/.venv/bin/python -m scheduler     --config /etc/agente-u/scheduler.toml --status
```

Incluye de dónde salen las reglas en vigor:

```
Reminders (from scheduler.toml), checked every 1h:
  [1h] when between 0h and 1h remain -> assignment_due_1h
  [6h] when between 1h and 6h remain -> assignment_due_6h
  [12h] when between 6h and 12h remain -> assignment_due_12h
  [24h] when between 12h and 24h remain -> assignment_due_24h
```

Si el estudiante ha guardado las suyas, dirá `(set by the student)`.

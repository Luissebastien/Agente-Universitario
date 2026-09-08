# Agente Universitario

Agente Universitario es un asistente de IA personal diseñado para ayudar a gestionar y automatizar la vida universitaria.

El proyecto busca conectar plataformas académicas como Moodle con un asistente inteligente capaz de organizar información, procesar materiales de los cursos, realizar seguimiento de tareas y fechas límite, enviar notificaciones y, eventualmente, automatizar determinadas tareas académicas con aprobación del usuario.

El nombre actual del asistente es **Agente U**.

---

## Visión

La información universitaria suele estar distribuida entre diferentes cursos, plataformas, documentos, tareas, anuncios y otras fuentes.

Agente U busca reunir esta información en un único asistente capaz de:

* monitorear información académica;
* realizar seguimiento de tareas y fechas límite;
* procesar materiales de los cursos;
* resumir y organizar contenido relevante;
* responder preguntas sobre cursos y materiales;
* enviar notificaciones útiles;
* ayudar a priorizar el trabajo académico;
* preparar acciones para el usuario;
* eventualmente automatizar determinadas tareas repetitivas.

El objetivo a largo plazo no es simplemente construir un chatbot, sino un agente universitario personal capaz de comprender el entorno académico del usuario y ayudar a gestionarlo.

---

## Estado actual

**Desarrollo inicial.**

Actualmente, el proyecto está enfocado en establecer su base y desarrollar la primera integración con Moodle.

Muchas decisiones técnicas permanecen deliberadamente abiertas hasta comprender mejor los requisitos reales.

---

## Desarrollo previsto

El proyecto evolucionará aproximadamente siguiendo estas etapas:

```text
Base del proyecto
        ↓
Integración con Moodle
        ↓
Recolección de información académica
        ↓
Interfaz con Telegram
        ↓
Procesamiento de materiales
        ↓
Knowledge base / RAG
        ↓
Integración con LLM
        ↓
Automatización
        ↓
Agente U
```

Esta es una dirección general de desarrollo y no una arquitectura técnica fija.

La implementación puede cambiar a medida que el proyecto avance.

---

## Estructura del proyecto

```text
Agente-Universitario/
│
├── .ai/                 # Contexto privado para el desarrollo con IA
│
├── docs/                # Documentación pública del proyecto
│
├── src/
│   ├── agent/           # Lógica del agente
│   ├── database/        # Persistencia de datos
│   ├── llm/             # Integración con LLM
│   ├── moodle/          # Integración con Moodle
│   ├── notifications/  # Notificaciones y mensajería
│   ├── rag/             # Sistemas de recuperación y conocimiento
│   └── scheduler/       # Tareas programadas
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── sentinel/        # Pruebas enfocadas en confiabilidad
│
├── .env.example         # Ejemplo de configuración del entorno
├── .gitignore
└── README.md
```

La estructura evolucionará a medida que aparezcan requisitos reales.

---

## Tecnología

El lenguaje principal del proyecto es actualmente **Python**.

El proyecto priorizará inicialmente soluciones simples y confiables en lugar de introducir complejidad arquitectónica innecesaria.

Frameworks, bases de datos, proveedores de LLM, infraestructura de hosting y otras tecnologías específicas serán seleccionados a medida que sus requisitos sean comprendidos.

---

## Privacidad

Agente U está pensado inicialmente para uso personal y puede procesar información académica privada.

El repositorio público nunca debe contener:

* contraseñas;
* API keys;
* authentication tokens;
* información académica privada;
* datos personales;
* documentos privados;
* otros datos sensibles utilizados durante la ejecución.

La configuración privada del entorno debe mantenerse fuera del repositorio público.

---

## Automatización y control del usuario

Agente U está diseñado para automatizar tareas universitarias repetitivas manteniendo al usuario en control de las acciones importantes.

El sistema podrá eventualmente preparar acciones de forma automática, pero las acciones externas de importancia deberán requerir aprobación explícita del usuario, salvo que esta política sea modificada intencionalmente en el futuro.

---

## Relaciones entre cursos

Los cursos se consideran independientes por defecto.

Cuando determinados cursos estén intencionalmente relacionados, Agente U podrá utilizar relaciones definidas por el usuario para permitir referencias relevantes entre ellos.

Estas relaciones podrán establecerse al comienzo de cada período académico.

---

## Desarrollo

Agente Universitario se desarrolla utilizando programación asistida por IA.

El proyecto mantiene un contexto y unas reglas privadas de desarrollo para mantener consistente y revisable el trabajo realizado con herramientas de IA.

El repositorio público contiene el proyecto y su documentación pública, mientras que el contexto privado utilizado durante el desarrollo con IA se mantiene excluido deliberadamente.

---

## License

Todavía no se ha seleccionado una licencia.

---

## Disclaimer

Este proyecto es experimental y está pensado principalmente como un proyecto de desarrollo personal.

Las funcionalidades, arquitectura y comportamiento pueden cambiar considerablemente durante el desarrollo.

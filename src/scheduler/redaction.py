from __future__ import annotations

import logging
import os
import re

# A token as a query parameter of any URL that ends up in an error/log line.
_TOKEN_PARAM_RE = re.compile(r"((?:ws)?token=)[^&\s'\"<>]+", re.IGNORECASE)
# A Telegram bot token has the shape '<bot id>:<35 url-safe characters>' and
# travels inside the URL *path* ('/bot<token>/sendMessage'), not in a header
# or a query parameter - so it can surface in anything that quotes that URL,
# where the rule above would not see it. Matched by shape rather than by
# value so it is redacted even when it reaches a process that does not have
# TELEGRAM_BOT_TOKEN set (a copied log line, a pasted traceback).
_TELEGRAM_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
# Secrets replaced literally, by value, wherever they appear.
_SECRET_ENV_VARS = ("MOODLE_TOKEN", "TELEGRAM_BOT_TOKEN")
# A literal secret is replaced only when it looks like a real one: replacing
# an empty or tiny value would mangle every message
# ('abc'.replace('', '***') == '***a***b***c***'). Moodle tokens are 32 hex chars.
_MIN_LITERAL_TOKEN_LENGTH = 8
_MAX_ERROR_LENGTH = 1000


def redact(text: str) -> str:
    text = _TOKEN_PARAM_RE.sub(r"\1***", text)
    text = _TELEGRAM_TOKEN_RE.sub("***", text)
    for variable in _SECRET_ENV_VARS:
        secret = os.environ.get(variable, "").strip()
        if len(secret) >= _MIN_LITERAL_TOKEN_LENGTH:
            text = text.replace(secret, "***")
    return text


def safe_error(exc: BaseException) -> str:
    """The single string form of an error that may be stored in execution
    history, logged, or put in a notification: redacted and bounded."""
    text = redact(f"{type(exc).__name__}: {exc}")
    if len(text) > _MAX_ERROR_LENGTH:
        text = text[: _MAX_ERROR_LENGTH - 3] + "..."
    return text


class SecretRedactingFilter(logging.Filter):
    """Handler-level filter: redacts every record from every logger, including
    formatted tracebacks, before any handler writes it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True

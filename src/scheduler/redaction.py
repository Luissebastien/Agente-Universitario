from __future__ import annotations

import logging
import os
import re

# A token as a query parameter of any URL that ends up in an error/log line.
_TOKEN_PARAM_RE = re.compile(r"((?:ws)?token=)[^&\s'\"<>]+", re.IGNORECASE)
# The literal MOODLE_TOKEN is replaced only when it looks like a real token:
# replacing an empty or tiny value would mangle every message
# ('abc'.replace('', '***') == '***a***b***c***'). Moodle tokens are 32 hex chars.
_MIN_LITERAL_TOKEN_LENGTH = 8
_MAX_ERROR_LENGTH = 1000


def redact(text: str) -> str:
    text = _TOKEN_PARAM_RE.sub(r"\1***", text)
    token = os.environ.get("MOODLE_TOKEN", "").strip()
    if len(token) >= _MIN_LITERAL_TOKEN_LENGTH:
        text = text.replace(token, "***")
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

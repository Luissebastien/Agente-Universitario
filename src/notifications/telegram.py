"""Telegram delivery for notifications (outbound only).

This module knows HOW to put a message in front of the student, never WHAT
to say or WHY - that stays in NotificationService (DEC-005: Telegram-specific
code is kept at the edge, so a future interface replaces it without touching
the core).

It receives no messages and serves no webhook: nothing here listens.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from notifications.models import Notification
from notifications.providers import NotificationProvider

logger = logging.getLogger(__name__)

# Fixed, not configurable. The bot token is part of the URL path, so the one
# host it may ever be sent to is a property of the code, not of the
# environment: an operator (or a tampered env file) must not be able to
# redirect an authenticated request somewhere else. HTTPS only, no fallback.
TELEGRAM_API_BASE = "https://api.telegram.org"

_DEFAULT_TIMEOUT = 15.0
# Telegram rejects a sendMessage text longer than 4096 characters.
MAX_MESSAGE_CHARS = 4096
# Enough of an error response to identify the cause, bounded so a broken or
# hostile endpoint cannot push an arbitrary amount of text into the logs.
_MAX_RESPONSE_BYTES = 4096
_MAX_DESCRIPTION_CHARS = 200


class TelegramDeliveryError(Exception):
    """A notification could not be delivered to Telegram.

    Its message is built ONLY from values this module controls - the
    exception type, the HTTP status, Telegram's own `description` - and never
    from a string that might quote the request URL, because the bot token is
    inside that URL. That is also why every handler below re-raises with
    `from None`: a chained exception would put the original (possibly
    URL-bearing) message into the traceback.
    """


class TelegramNotificationProvider(NotificationProvider):
    """Sends each notification as one Telegram message, over HTTPS, using the
    standard library only - same approach as the Moodle client, no new
    dependency.

    Delivery keeps NotificationService's at-least-once semantics: send()
    either returns (Telegram accepted the message) or raises, and only a clean
    return lets the notification be recorded as sent. A crash between Telegram
    accepting and the row being written means the message is sent again next
    run, so the student may occasionally see a duplicate. That is deliberate:
    a duplicate reminder is harmless, a silently lost one is not.
    """

    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        urlopen: Callable = urllib.request.urlopen,
    ) -> None:
        """`urlopen` is a seam for tests only; production always uses urllib's."""
        if not token:
            raise ValueError("token is required")
        if not chat_id:
            raise ValueError("chat_id is required")
        self._chat_id = chat_id
        self._timeout = timeout
        self._urlopen = urlopen
        # Built once, never logged. Quoting keeps a malformed token from
        # changing the path structure (a '/' in it would otherwise point the
        # request at a different method). ':' stays literal: it separates the
        # bot id from the secret in every real token, and is legal in a path
        # segment - percent-encoding it would change the token Telegram sees.
        quoted = urllib.parse.quote(token, safe=":")
        self._url = f"{TELEGRAM_API_BASE}/bot{quoted}/sendMessage"

    def send(self, notification: Notification) -> None:
        payload = json.dumps(
            {"chat_id": self._chat_id, "text": _message_text(notification)}
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                body = response.read(_MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            raise TelegramDeliveryError(
                f"Telegram rejected the message (HTTP {exc.code})"
                f"{_description_of(_body_of(exc))}"
            ) from None
        except Exception as exc:  # noqa: BLE001 - see TelegramDeliveryError
            # Timeouts, DNS and TLS failures, and anything else urllib raises:
            # only the type is kept, because several of these embed the URL.
            raise TelegramDeliveryError(
                f"Could not reach Telegram ({type(exc).__name__})"
            ) from None

        _raise_for_api_error(body)
        # Deliberately no title/body here: the academic content is what the
        # student reads in Telegram, and it does not also need to be in the
        # system log - which keeps enough to tell which notification this was.
        logger.info(
            "Telegram notification delivered: %s for %s",
            notification.notification_type,
            notification.subject_key,
        )


def telegram_provider_from_env(
    *,
    token_var: str = "TELEGRAM_BOT_TOKEN",
    chat_id_var: str = "TELEGRAM_CHAT_ID",
    **kwargs,
) -> TelegramNotificationProvider | None:
    """The configured provider, or None when Telegram is not configured.

    Returning None rather than raising is what makes Telegram optional: the
    system has to start and keep working without it (development, a host where
    it is not set up yet, and rollback - which is just removing these two
    variables). Credentials are read from the environment per process, never
    from scheduler.toml and never from the database, exactly like the Moodle
    token.
    """
    token = os.environ.get(token_var, "").strip()
    chat_id = os.environ.get(chat_id_var, "").strip()
    if not token and not chat_id:
        return None
    if not token or not chat_id:
        # Half-configured is almost always a mistake, but it must not stop the
        # process from starting. Neither value is named in the warning.
        missing = token_var if not token else chat_id_var
        logger.warning(
            "Telegram is only half configured (%s is missing): "
            "falling back to the log channel",
            missing,
        )
        return None
    return TelegramNotificationProvider(token, chat_id, **kwargs)


def _message_text(notification: Notification) -> str:
    """The message as plain text.

    No parse_mode is sent, so Telegram does no Markdown/HTML parsing: a course
    or assignment name containing '_', '*' or '<' is delivered as written
    instead of being rejected as malformed markup.
    """
    text = f"{notification.title}\n{notification.body}"
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"
    return text


def _raise_for_api_error(body: bytes) -> None:
    """Telegram answers with {"ok": true} only when it really sent the message."""
    try:
        data = json.loads(body)
    except ValueError:
        raise TelegramDeliveryError(
            "Telegram returned a response that is not JSON"
        ) from None
    if not (isinstance(data, dict) and data.get("ok") is True):
        raise TelegramDeliveryError(
            f"Telegram did not accept the message{_description_of(data)}"
        )


def _body_of(exc: urllib.error.HTTPError) -> object:
    """Telegram's JSON error body, or None when it cannot be read or parsed."""
    try:
        return json.loads(exc.read(_MAX_RESPONSE_BYTES))
    except Exception:  # noqa: BLE001 - the status alone is still a usable error
        return None


def _description_of(data: object) -> str:
    """Telegram's human-readable reason ('chat not found', 'Unauthorized'),
    bounded, as a suffix ready to append - or '' when there is none."""
    if not isinstance(data, dict):
        return ""
    description = data.get("description")
    if not isinstance(description, str) or not description:
        return ""
    return f": {description[:_MAX_DESCRIPTION_CHARS]}"

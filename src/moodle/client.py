from __future__ import annotations

import http.client
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from moodle.exceptions import (
    MoodleAPIError,
    MoodleAuthenticationError,
    MoodleConnectionError,
    MoodleHTTPError,
    MoodleUntrustedURLError,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15.0
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 1.0
_TRANSIENT_HTTP_STATUSES = frozenset({502, 503, 504})

# Moodle errorcodes observed (via login/token.php and webservice/rest/server.php
# source) to mean "the credential itself is invalid", as opposed to a generic
# API error (bad params, missing capability, etc.).
_AUTH_ERRORCODES = frozenset({"invalidtoken", "invalidlogin"})


def _redact_url(url: str) -> str:
    """Return url with any 'token' query parameter value replaced, for safe logging."""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [(key, "***" if key.lower() == "token" else value) for key, value in query]
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(redacted)))


def _flatten_params(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten nested dict/list params into Moodle's PHP-style form field names.

    E.g. {"options": [{"name": "x", "value": 1}]} becomes
    {"options[0][name]": "x", "options[0][value]": "1"}.
    """
    flat: dict[str, str] = {}
    for key, value in params.items():
        field = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten_params(value, field))
        elif isinstance(value, (list, tuple)):
            flat.update(_flatten_params(dict(enumerate(value)), field))
        elif isinstance(value, bool):
            flat[field] = "1" if value else "0"
        elif value is None:
            flat[field] = ""
        else:
            flat[field] = str(value)
    return flat


def _raise_for_api_error(data: Any) -> None:
    if isinstance(data, dict) and "exception" in data and "errorcode" in data:
        errorcode = str(data.get("errorcode", "unknown"))
        message = str(data.get("message", ""))
        if errorcode in _AUTH_ERRORCODES:
            raise MoodleAuthenticationError(errorcode, message)
        raise MoodleAPIError(errorcode, message)


class MoodleClient:
    """Thin client for a Moodle site's REST Web Service API.

    Responsible only for talking to Moodle: building requests, authenticating
    with a token, and turning HTTP/API failures into typed exceptions. It has
    no knowledge of courses, storage, or Agente U's business logic.
    """

    def __init__(self, base_url: str, token: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required")

        parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported base_url scheme: {parsed.scheme!r}")

        self._scheme = parsed.scheme
        self._host = parsed.netloc
        # Hostname only (no port/userinfo), used to decide which URLs the
        # token may ever be sent to. Derived via urlsplit rather than string
        # matching so userinfo/port formatting can't confuse the comparison.
        self._trusted_hostname = (parsed.hostname or "").lower()
        self._rest_path = f"{parsed.path}/webservice/rest/server.php"
        self._token = token
        self._timeout = timeout
        self._connection: http.client.HTTPConnection | None = None
        self._site_userid: int | None = None

    @classmethod
    def from_env(
        cls,
        *,
        url_var: str = "MOODLE_URL",
        token_var: str = "MOODLE_TOKEN",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> MoodleClient:
        base_url = os.environ.get(url_var)
        token = os.environ.get(token_var)
        if not base_url or not token:
            raise ValueError(f"{url_var} and {token_var} must both be set in the environment")
        return cls(base_url, token, timeout=timeout)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> MoodleClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- core request mechanism ----------------------------------------------

    def _get_connection(self) -> http.client.HTTPConnection:
        if self._connection is None:
            conn_cls = (
                http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
            )
            self._connection = conn_cls(self._host, timeout=self._timeout)
        return self._connection

    def call(self, function_name: str, params: dict[str, Any] | None = None) -> Any:
        """Call a Moodle web service function and return its parsed JSON result.

        Raises MoodleConnectionError, MoodleHTTPError, MoodleAuthenticationError,
        or MoodleAPIError on failure. Never returns silently corrupted data.
        """
        body_params = {
            "wstoken": self._token,
            "wsfunction": function_name,
            "moodlewsrestformat": "json",
        }
        body_params.update(_flatten_params(params or {}))
        body = urllib.parse.urlencode(body_params)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                conn = self._get_connection()
                conn.request("POST", self._rest_path, body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                status = response.status
            except (http.client.HTTPException, OSError) as exc:
                # Drop the (possibly broken, e.g. idle-closed) connection so the
                # next attempt reconnects cleanly.
                self.close()
                if attempt > _MAX_RETRIES:
                    raise MoodleConnectionError(
                        f"Could not reach Moodle while calling '{function_name}'"
                    ) from exc
                logger.warning(
                    "Moodle call %s failed on attempt %d, retrying: %s",
                    function_name, attempt, exc.__class__.__name__,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                continue

            duration_ms = (time.monotonic() - started) * 1000
            logger.info("Moodle call %s -> HTTP %d (%.0f ms)", function_name, status, duration_ms)

            if status in _TRANSIENT_HTTP_STATUSES and attempt <= _MAX_RETRIES:
                logger.warning("Moodle returned HTTP %d for %s, retrying", status, function_name)
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                continue

            break

        if status != 200:
            raise MoodleHTTPError(status)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MoodleAPIError("invalidresponse", "Moodle response was not valid JSON") from exc

        _raise_for_api_error(data)
        return data

    # -- verified public operations -------------------------------------------

    def get_site_info(self) -> dict[str, Any]:
        """core_webservice_get_site_info: identity, version, and authorized functions."""
        data = self.call("core_webservice_get_site_info")
        userid = data.get("userid")
        if isinstance(userid, int):
            self._site_userid = userid
        return data

    def get_courses(self, user_id: int | None = None) -> list[dict[str, Any]]:
        """core_enrol_get_users_courses: courses the authenticated user can access.

        If user_id is omitted, uses the id cached from an earlier get_site_info()
        call, calling it first if necessary.
        """
        if user_id is None:
            user_id = self._site_userid or self.get_site_info().get("userid")
        if user_id is None:
            raise MoodleAPIError("missinguserid", "Could not determine the authenticated user's id")
        return self.call("core_enrol_get_users_courses", {"userid": user_id})

    def get_course_contents(self, course_id: int) -> list[dict[str, Any]]:
        """core_course_get_contents: sections, modules, and resources of a course."""
        return self.call("core_course_get_contents", {"courseid": course_id})

    def _assert_trusted_file_url(self, file_url: str, parsed: urllib.parse.SplitResult) -> None:
        """Refuse to send the token anywhere but the configured Moodle host.

        Compares the exact hostname (not a suffix/substring check, so e.g.
        'evil-intec.edu.do' or an unrelated subdomain is rejected), and if the
        configured site is https, also refuses to downgrade to plain http.
        """
        hostname = (parsed.hostname or "").lower()
        scheme_ok = parsed.scheme == self._scheme or (
            parsed.scheme == "https" and self._scheme == "http"
        )
        if parsed.scheme not in ("http", "https") or hostname != self._trusted_hostname or not scheme_ok:
            raise MoodleUntrustedURLError(
                f"Refusing to send the Moodle token to untrusted URL: {_redact_url(file_url)}"
            )

    def download_file(self, file_url: str, *, timeout: float | None = None) -> bytes:
        """Download a Moodle file (e.g. a webservice/pluginfile.php URL) using the token.

        Preserves any existing query parameters on file_url; replaces or adds
        the 'token' parameter rather than blindly appending one. The token is
        only ever sent to the Moodle host configured via base_url.
        """
        parsed = urllib.parse.urlsplit(file_url)
        self._assert_trusted_file_url(file_url, parsed)

        query = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() != "token"
        ]
        query.append(("token", self._token))
        authenticated_url = urllib.parse.urlunsplit(
            parsed._replace(query=urllib.parse.urlencode(query))
        )

        try:
            with urllib.request.urlopen(
                authenticated_url, timeout=timeout or self._timeout
            ) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            raise MoodleHTTPError(exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise MoodleConnectionError(
                f"Could not download Moodle file: {_redact_url(file_url)}"
            ) from exc

        # Moodle serves API errors (e.g. an invalid/expired token) as HTTP 200
        # with a JSON error body here too, mirroring the REST endpoint.
        if data[:1] == b"{":
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and "exception" in payload:
                _raise_for_api_error(payload)

        return data

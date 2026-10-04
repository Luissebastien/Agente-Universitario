class MoodleError(Exception):
    """Base exception for all Moodle integration failures."""


class MoodleConnectionError(MoodleError):
    """The HTTP request to Moodle could not be completed (network/timeout)."""


class MoodleHTTPError(MoodleError):
    """Moodle responded with a non-200 HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Moodle returned HTTP {status_code}")


class MoodleAPIError(MoodleError):
    """Moodle responded with HTTP 200 but the JSON body is an API-level error."""

    def __init__(self, errorcode: str, message: str) -> None:
        self.errorcode = errorcode
        super().__init__(f"Moodle API error [{errorcode}]: {message}")


class MoodleAuthenticationError(MoodleAPIError):
    """The token was rejected by Moodle (invalid, expired, or missing)."""


class MoodleResourceTooLargeError(MoodleError):
    """A file is larger than the per-download limit, so it was never read.

    Carries the size seen and the limit in force so the caller can record
    both: the limit may be retuned later, and stored history has to stay
    interpretable when it is. Neither the URL nor the filename is included -
    this message reaches logs and execution history.
    """

    def __init__(self, size_bytes: int | None, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        seen = f"{size_bytes} bytes" if size_bytes is not None else "an undeclared size"
        super().__init__(
            f"Refusing to download a Moodle file of {seen}: over the {limit_bytes}-byte limit"
        )


class MoodleUntrustedURLError(MoodleError):
    """A file URL does not belong to the configured Moodle host.

    Raised before any HTTP request is made, so the token is never sent
    anywhere other than the Moodle instance configured via base_url.
    """

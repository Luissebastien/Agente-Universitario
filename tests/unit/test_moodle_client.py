import json
import unittest
from unittest.mock import MagicMock, patch

from moodle.client import MoodleClient, _flatten_params, _redact_url
from moodle.exceptions import (
    MoodleAPIError,
    MoodleAuthenticationError,
    MoodleConnectionError,
    MoodleHTTPError,
    MoodleUntrustedURLError,
)

FAKE_TOKEN = "test-token-should-never-leak-1234"


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


def make_fake_connection(status: int, payload) -> MagicMock:
    body = json.dumps(payload).encode() if not isinstance(payload, (bytes, str)) else payload
    if isinstance(body, str):
        body = body.encode()
    conn = MagicMock()
    conn.getresponse.return_value = FakeResponse(status, body)
    return conn


class UrlConstructionTests(unittest.TestCase):
    def test_trailing_slash_does_not_change_rest_path(self) -> None:
        with_slash = MoodleClient("https://campusvirtual.intec.edu.do/", FAKE_TOKEN)
        without_slash = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)
        self.assertEqual(with_slash._rest_path, without_slash._rest_path)
        self.assertEqual(with_slash._host, without_slash._host)
        self.assertEqual(with_slash._rest_path, "/webservice/rest/server.php")

    def test_missing_base_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            MoodleClient("", FAKE_TOKEN)

    def test_missing_token_raises(self) -> None:
        with self.assertRaises(ValueError):
            MoodleClient("https://campusvirtual.intec.edu.do", "")

    def test_unsupported_scheme_raises(self) -> None:
        with self.assertRaises(ValueError):
            MoodleClient("ftp://campusvirtual.intec.edu.do", FAKE_TOKEN)


class SuccessfulCallTests(unittest.TestCase):
    @patch("moodle.client.http.client.HTTPSConnection")
    def test_call_returns_parsed_json(self, mock_conn_cls: MagicMock) -> None:
        mock_conn_cls.return_value = make_fake_connection(200, {"sitename": "Campus Virtual INTEC"})
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        result = client.get_site_info()

        self.assertEqual(result, {"sitename": "Campus Virtual INTEC"})

    @patch("moodle.client.http.client.HTTPSConnection")
    def test_call_sends_required_fields(self, mock_conn_cls: MagicMock) -> None:
        fake_conn = make_fake_connection(200, {"ok": True})
        mock_conn_cls.return_value = fake_conn
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        client.call("core_webservice_get_site_info")

        _, kwargs = fake_conn.request.call_args
        sent_body = fake_conn.request.call_args.kwargs.get("body") or fake_conn.request.call_args[1]["body"]
        self.assertIn("wstoken=" + FAKE_TOKEN, sent_body)
        self.assertIn("wsfunction=core_webservice_get_site_info", sent_body)
        self.assertIn("moodlewsrestformat=json", sent_body)


class ApiErrorTests(unittest.TestCase):
    @patch("moodle.client.http.client.HTTPSConnection")
    def test_invalidtoken_raises_authentication_error(self, mock_conn_cls: MagicMock) -> None:
        mock_conn_cls.return_value = make_fake_connection(
            200, {"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "bad token"}
        )
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        with self.assertRaises(MoodleAuthenticationError):
            client.call("core_webservice_get_site_info")

    @patch("moodle.client.http.client.HTTPSConnection")
    def test_other_errorcode_raises_generic_api_error(self, mock_conn_cls: MagicMock) -> None:
        mock_conn_cls.return_value = make_fake_connection(
            200, {"exception": "moodle_exception", "errorcode": "invalidparameter", "message": "bad param"}
        )
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        with self.assertRaises(MoodleAPIError) as ctx:
            client.call("core_course_get_contents")

        self.assertNotIsInstance(ctx.exception, MoodleAuthenticationError)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))


class HttpErrorTests(unittest.TestCase):
    @patch("moodle.client.http.client.HTTPSConnection")
    def test_non_200_status_raises_http_error(self, mock_conn_cls: MagicMock) -> None:
        mock_conn_cls.return_value = make_fake_connection(404, b"Not Found")
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        with self.assertRaises(MoodleHTTPError) as ctx:
            client.call("core_webservice_get_site_info")

        self.assertEqual(ctx.exception.status_code, 404)


class ConnectionFailureTests(unittest.TestCase):
    @patch("moodle.client.time.sleep")
    @patch("moodle.client.http.client.HTTPSConnection")
    def test_connection_error_retries_then_raises(
        self, mock_conn_cls: MagicMock, mock_sleep: MagicMock
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.request.side_effect = OSError("connection reset")
        mock_conn_cls.return_value = mock_conn
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        with self.assertRaises(MoodleConnectionError) as ctx:
            client.call("core_webservice_get_site_info")

        self.assertEqual(mock_conn.request.call_count, 3)  # 1 + _MAX_RETRIES
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))


class TokenRedactionTests(unittest.TestCase):
    def test_redact_url_hides_token_keeps_other_params(self) -> None:
        url = f"https://campusvirtual.intec.edu.do/file.php?forcedownload=1&token={FAKE_TOKEN}"

        redacted = _redact_url(url)

        self.assertNotIn(FAKE_TOKEN, redacted)
        self.assertIn("forcedownload=1", redacted)

    @patch("moodle.client.urllib.request.urlopen")
    def test_download_connection_error_does_not_leak_token(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = OSError("network unreachable")
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        with self.assertRaises(MoodleConnectionError) as ctx:
            client.download_file("https://campusvirtual.intec.edu.do/file.php")

        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))


class FlattenParamsTests(unittest.TestCase):
    def test_flat_scalars(self) -> None:
        self.assertEqual(_flatten_params({"courseid": 5}), {"courseid": "5"})

    def test_nested_list_of_dicts(self) -> None:
        result = _flatten_params({"options": [{"name": "x", "value": 1}]})
        self.assertEqual(result, {"options[0][name]": "x", "options[0][value]": "1"})

    def test_bool_and_none(self) -> None:
        result = _flatten_params({"flag": True, "missing": None})
        self.assertEqual(result, {"flag": "1", "missing": ""})


class GetCoursesTests(unittest.TestCase):
    @patch("moodle.client.http.client.HTTPSConnection")
    def test_uses_cached_userid_from_site_info(self, mock_conn_cls: MagicMock) -> None:
        conn = MagicMock()
        conn.getresponse.side_effect = [
            FakeResponse(200, json.dumps({"userid": 15465}).encode()),
            FakeResponse(200, json.dumps([{"id": 22055, "shortname": "IDS343"}]).encode()),
        ]
        mock_conn_cls.return_value = conn
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        client.get_site_info()
        courses = client.get_courses()

        self.assertEqual(courses, [{"id": 22055, "shortname": "IDS343"}])
        second_call_body = conn.request.call_args_list[1].kwargs["body"]
        self.assertIn("userid=15465", second_call_body)

    @patch("moodle.client.http.client.HTTPSConnection")
    def test_explicit_user_id_skips_site_info_call(self, mock_conn_cls: MagicMock) -> None:
        mock_conn_cls.return_value = make_fake_connection(200, [])
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        client.get_courses(user_id=999)

        body = mock_conn_cls.return_value.request.call_args.kwargs["body"]
        self.assertIn("userid=999", body)
        self.assertIn("wsfunction=core_enrol_get_users_courses", body)


class GetCourseContentsTests(unittest.TestCase):
    @patch("moodle.client.http.client.HTTPSConnection")
    def test_sends_courseid_and_returns_data(self, mock_conn_cls: MagicMock) -> None:
        mock_conn_cls.return_value = make_fake_connection(200, [{"id": 1, "modules": []}])
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        result = client.get_course_contents(22055)

        self.assertEqual(result, [{"id": 1, "modules": []}])
        body = mock_conn_cls.return_value.request.call_args.kwargs["body"]
        self.assertIn("courseid=22055", body)
        self.assertIn("wsfunction=core_course_get_contents", body)


class DownloadFileTests(unittest.TestCase):
    @patch("moodle.client.urllib.request.urlopen")
    def test_appends_token_to_plain_url(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b"%PDF-1.4 fake pdf bytes"
        mock_urlopen.return_value.__enter__.return_value = mock_response
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        data = client.download_file("https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/x.pdf")

        self.assertEqual(data, b"%PDF-1.4 fake pdf bytes")
        requested_url = mock_urlopen.call_args[0][0]
        self.assertIn(f"token={FAKE_TOKEN}", requested_url)

    @patch("moodle.client.urllib.request.urlopen")
    def test_preserves_existing_query_params_and_replaces_foreign_token(
        self, mock_urlopen: MagicMock
    ) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b"binary-content"
        mock_urlopen.return_value.__enter__.return_value = mock_response
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        client.download_file(
            "https://campusvirtual.intec.edu.do/file.php?forcedownload=1&token=someone-elses-token"
        )

        requested_url = mock_urlopen.call_args[0][0]
        self.assertIn("forcedownload=1", requested_url)
        self.assertIn(f"token={FAKE_TOKEN}", requested_url)
        self.assertNotIn("someone-elses-token", requested_url)
        self.assertEqual(requested_url.count("token="), 1)

    @patch("moodle.client.urllib.request.urlopen")
    def test_api_error_body_with_http_200_raises(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "bad token"}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        with self.assertRaises(MoodleAuthenticationError):
            client.download_file("https://campusvirtual.intec.edu.do/file.php")


class DownloadFileHostRestrictionTests(unittest.TestCase):
    @patch("moodle.client.urllib.request.urlopen")
    def test_configured_host_receives_token(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b"file-bytes"
        mock_urlopen.return_value.__enter__.return_value = mock_response
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        data = client.download_file(
            "https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/mod_resource/content/1/x.pdf"
        )

        self.assertEqual(data, b"file-bytes")
        requested_url = mock_urlopen.call_args[0][0]
        self.assertIn(f"token={FAKE_TOKEN}", requested_url)

    @patch("moodle.client.urllib.request.urlopen")
    def test_external_url_is_rejected_without_any_request(self, mock_urlopen: MagicMock) -> None:
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        with self.assertRaises(MoodleUntrustedURLError):
            client.download_file("https://example.com/file.pdf")

        mock_urlopen.assert_not_called()

    @patch("moodle.client.urllib.request.urlopen")
    def test_different_subdomain_is_rejected(self, mock_urlopen: MagicMock) -> None:
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        # Must not be accepted just because it ends with "intec.edu.do".
        with self.assertRaises(MoodleUntrustedURLError):
            client.download_file("https://files.intec.edu.do/file.pdf")

        mock_urlopen.assert_not_called()

    @patch("moodle.client.urllib.request.urlopen")
    def test_lookalike_host_is_rejected(self, mock_urlopen: MagicMock) -> None:
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        # Must not be accepted just because "intec.edu.do" appears as a substring.
        with self.assertRaises(MoodleUntrustedURLError):
            client.download_file("https://campusvirtual.intec.edu.do.evil.com/file.pdf")

        mock_urlopen.assert_not_called()

    @patch("moodle.client.urllib.request.urlopen")
    def test_https_site_rejects_downgrade_to_http(self, mock_urlopen: MagicMock) -> None:
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        with self.assertRaises(MoodleUntrustedURLError):
            client.download_file("http://campusvirtual.intec.edu.do/file.pdf")

        mock_urlopen.assert_not_called()

    @patch("moodle.client.urllib.request.urlopen")
    def test_rejection_message_does_not_contain_token(self, mock_urlopen: MagicMock) -> None:
        client = MoodleClient("https://campusvirtual.intec.edu.do", FAKE_TOKEN)

        # Even an attacker-crafted URL carrying its own "token" param must not
        # cause our token, or the foreign one, to leak into the error message.
        with self.assertRaises(MoodleUntrustedURLError) as ctx:
            client.download_file("https://example.com/steal?token=someone-elses-token")

        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
        self.assertNotIn("someone-elses-token", str(ctx.exception))
        mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

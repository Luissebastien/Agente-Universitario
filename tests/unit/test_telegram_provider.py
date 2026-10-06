"""Telegram as an outbound notification channel.

Nothing here touches the network: the provider takes its `urlopen` as a seam,
so every test inspects the exact request that would have been sent.
"""
import email.message
import io
import json
import os
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from database import moodle_repository as repo
from database import scheduler_repository as history
from database.db import connect
from moodle.models import Assignment, AssignmentSubmissionStatus, Course
from notifications.models import NOTIFICATION_ASSIGNMENT_DUE_SOON, Notification
from notifications.providers import LogNotificationProvider
from notifications.service import NotificationService
from notifications.telegram import (
    MAX_MESSAGE_CHARS,
    TELEGRAM_API_BASE,
    TelegramDeliveryError,
    TelegramNotificationProvider,
    telegram_provider_from_env,
)
from scheduler.app import notification_provider
from scheduler.config import INGESTION, MOODLE_SYNC, PIPELINE
from scheduler.jobs import TRIGGER_MANUAL
from scheduler.redaction import redact, safe_error
from scheduler.scheduler import MAX_ATTEMPTS, Scheduler

from .scheduler_fakes import TZ, FakeJob, FakeTime, make_config

# Shaped exactly like a real bot token (digits, colon, 35 url-safe chars) so
# the redaction rules are exercised honestly - but it is not one, and never
# was: the word 'fake' is inside the secret part on purpose.
FAKE_TOKEN = "123456789:AAHfakeTokenForTestsOnly0123456789A"
FAKE_CHAT_ID = "987654321"
OK_BODY = b'{"ok":true,"result":{"message_id":1}}'
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=TZ)


def a_notification(title: str = "Tarea por vencer: Proyecto", body: str = "Curso - vence manana") -> Notification:
    return Notification(
        notification_type=NOTIFICATION_ASSIGNMENT_DUE_SOON,
        subject_key="assignment:1",
        scheduled_for="2026-10-03T16:00:00+00:00",
        title=title,
        body=body,
    )


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int | None = None) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class FakeTransport:
    """Records the requests the provider makes instead of performing them."""

    def __init__(self, body: bytes = OK_BODY, error: BaseException | None = None) -> None:
        self.requests: list[urllib.request.Request] = []
        self._body = body
        self._error = error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeout = timeout
        if self._error is not None:
            raise self._error
        return FakeResponse(self._body)

    @property
    def last(self):
        return self.requests[-1]

    def payload(self) -> dict:
        return json.loads(self.last.data)


def http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    """An HTTPError built like the real one: its URL carries the bot token."""
    url = f"{TELEGRAM_API_BASE}/bot{FAKE_TOKEN}/sendMessage"
    return urllib.error.HTTPError(url, code, "Unauthorized", email.message.Message(), io.BytesIO(body))


class TelegramRequestTests(unittest.TestCase):
    """What exactly goes on the wire."""

    def setUp(self) -> None:
        self.transport = FakeTransport()
        self.provider = TelegramNotificationProvider(
            FAKE_TOKEN, FAKE_CHAT_ID, urlopen=self.transport
        )

    def test_payload_is_the_chat_and_the_text_and_nothing_else(self) -> None:
        self.provider.send(a_notification())

        payload = self.transport.payload()
        self.assertEqual(set(payload), {"chat_id", "text"})
        self.assertEqual(payload["chat_id"], FAKE_CHAT_ID)
        self.assertEqual(payload["text"], "Tarea por vencer: Proyecto\nCurso - vence manana")

    def test_no_parse_mode_so_markup_characters_survive(self) -> None:
        self.provider.send(a_notification(title="Tarea_1 *importante*", body="<ver> aqui"))

        payload = self.transport.payload()
        self.assertNotIn("parse_mode", payload)
        self.assertIn("Tarea_1 *importante*", payload["text"])
        self.assertIn("<ver> aqui", payload["text"])

    def test_endpoint_is_telegrams_send_message_over_https(self) -> None:
        self.provider.send(a_notification())

        self.assertEqual(
            self.transport.last.full_url,
            f"{TELEGRAM_API_BASE}/bot{FAKE_TOKEN}/sendMessage",
        )
        self.assertEqual(self.transport.last.method, "POST")
        self.assertEqual(self.transport.last.headers["Content-type"], "application/json")

    def test_the_only_reachable_endpoint_is_https_and_is_not_configurable(self) -> None:
        self.provider.send(a_notification())

        self.assertTrue(TELEGRAM_API_BASE.startswith("https://"))
        self.assertTrue(self.transport.last.full_url.startswith("https://"))
        # No constructor parameter, environment variable or configuration key
        # can point the provider anywhere else.
        self.assertNotIn("api_base", TelegramNotificationProvider.__init__.__code__.co_varnames)

    def test_a_malformed_token_cannot_change_which_method_is_called(self) -> None:
        provider = TelegramNotificationProvider(
            "123456789:secret/getUpdates", FAKE_CHAT_ID, urlopen=self.transport
        )
        provider.send(a_notification())

        url = self.transport.last.full_url
        self.assertTrue(url.endswith("/sendMessage"))
        self.assertNotIn("/getUpdates", url)

    def test_a_message_over_telegrams_limit_is_truncated(self) -> None:
        self.provider.send(a_notification(body="x" * 6000))

        text = self.transport.payload()["text"]
        self.assertEqual(len(text), MAX_MESSAGE_CHARS)
        self.assertTrue(text.endswith("…"))

    def test_empty_credentials_are_refused_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            TelegramNotificationProvider("", FAKE_CHAT_ID)
        with self.assertRaises(ValueError):
            TelegramNotificationProvider(FAKE_TOKEN, "")


class TelegramFailureTests(unittest.TestCase):
    """Every way a send can fail becomes one typed, token-free error."""

    def send_with(self, **kwargs) -> TelegramDeliveryError:
        provider = TelegramNotificationProvider(
            FAKE_TOKEN, FAKE_CHAT_ID, urlopen=FakeTransport(**kwargs)
        )
        with self.assertRaises(TelegramDeliveryError) as raised:
            provider.send(a_notification())
        return raised.exception

    def test_http_error_keeps_the_status_and_telegrams_reason(self) -> None:
        exc = self.send_with(error=http_error(401, b'{"ok":false,"description":"Unauthorized"}'))

        self.assertIn("401", str(exc))
        self.assertIn("Unauthorized", str(exc))

    def test_http_error_without_a_usable_body_still_reports_the_status(self) -> None:
        exc = self.send_with(error=http_error(502, b"<html>Bad Gateway</html>"))

        self.assertIn("502", str(exc))

    def test_connection_failure_and_timeout_are_reported_by_type(self) -> None:
        unreachable = self.send_with(error=urllib.error.URLError("[Errno -2] Name or service not known"))
        timed_out = self.send_with(error=TimeoutError("timed out"))

        self.assertIn("URLError", str(unreachable))
        self.assertIn("TimeoutError", str(timed_out))

    def test_telegram_answering_not_ok_is_a_failure_not_a_success(self) -> None:
        exc = self.send_with(body=b'{"ok":false,"description":"chat not found"}')

        self.assertIn("chat not found", str(exc))

    def test_a_response_that_is_not_json_is_a_failure(self) -> None:
        exc = self.send_with(body=b"<html>502 Bad Gateway</html>")

        self.assertIn("not JSON", str(exc))


class TelegramSecretTests(unittest.TestCase):
    """The bot token lives in the URL path, so it has to survive no error path."""

    def assert_token_free(self, exc: BaseException) -> None:
        self.assertNotIn(FAKE_TOKEN, str(exc))
        self.assertNotIn(FAKE_TOKEN, repr(exc))
        self.assertNotIn(FAKE_TOKEN, safe_error(exc))
        # The chained cause is dropped on purpose: a chained HTTPError would
        # print its URL - token included - in the traceback.
        self.assertIsNone(exc.__cause__)

    def failures(self) -> list[BaseException]:
        errors = []
        for failure in (
            {"error": http_error(401, b'{"ok":false,"description":"Unauthorized"}')},
            {"error": urllib.error.URLError(f"failed for {TELEGRAM_API_BASE}/bot{FAKE_TOKEN}/sendMessage")},
            {"error": ValueError(f"invalid URL {TELEGRAM_API_BASE}/bot{FAKE_TOKEN}/sendMessage")},
            {"body": b'{"ok":false,"description":"no"}'},
            {"body": b"not json"},
        ):
            provider = TelegramNotificationProvider(
                FAKE_TOKEN, FAKE_CHAT_ID, urlopen=FakeTransport(**failure)
            )
            try:
                provider.send(a_notification())
            except TelegramDeliveryError as exc:
                errors.append(exc)
        return errors

    def test_no_failure_path_exposes_the_token(self) -> None:
        errors = self.failures()

        self.assertEqual(len(errors), 5)
        for exc in errors:
            self.assert_token_free(exc)

    def test_redaction_masks_a_token_by_shape_and_by_value(self) -> None:
        line = f"POST {TELEGRAM_API_BASE}/bot{FAKE_TOKEN}/sendMessage failed"

        # By shape: works in a process that does not have the variable set.
        with patch.dict(os.environ, {}, clear=True):
            by_shape = redact(line)
        # By value: the literal secret, wherever it appears.
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN}):
            by_value = redact(f"token is {FAKE_TOKEN}")

        self.assertNotIn(FAKE_TOKEN, by_shape)
        self.assertIn("/bot***/sendMessage", by_shape)
        self.assertNotIn(FAKE_TOKEN, by_value)

    def test_redaction_still_masks_the_moodle_token(self) -> None:
        moodle_token = "0123456789abcdef0123456789abcdef"
        with patch.dict(os.environ, {"MOODLE_TOKEN": moodle_token}):
            redacted = redact(f"GET https://x/pluginfile.php?token={moodle_token} ({moodle_token})")

        self.assertNotIn(moodle_token, redacted)
        self.assertIn("token=***", redacted)

    def test_no_real_credentials_are_committed(self) -> None:
        example = Path(__file__).resolve().parents[2] / ".env.example"
        lines = example.read_text(encoding="utf-8").splitlines()

        for variable in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            self.assertIn(f"{variable}=", lines, f"{variable} must be listed, with no value")


class TelegramLoggingTests(unittest.TestCase):
    """Telegram carries the academic content; the log keeps the technical trace."""

    def test_a_delivered_notification_is_logged_without_its_content(self) -> None:
        provider = TelegramNotificationProvider(FAKE_TOKEN, FAKE_CHAT_ID, urlopen=FakeTransport())
        notification = a_notification(
            title="Tarea por vencer: Proyecto Final", body="MATEMATICA BASICA - vence el 2026-10-03"
        )

        with self.assertLogs("notifications.telegram", level="INFO") as logs:
            provider.send(notification)

        output = "\n".join(logs.output)
        self.assertNotIn("Proyecto Final", output)
        self.assertNotIn("MATEMATICA BASICA", output)
        self.assertIn(notification.subject_key, output)
        self.assertIn(notification.notification_type, output)


class ProviderSelectionTests(unittest.TestCase):
    """Configured by environment alone, and never fatal."""

    def select(self, env: dict) -> object:
        with patch.dict(os.environ, env, clear=True):
            return notification_provider()

    def test_both_variables_select_telegram(self) -> None:
        provider = self.select({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": FAKE_CHAT_ID})

        self.assertIsInstance(provider, TelegramNotificationProvider)

    def test_no_configuration_falls_back_to_the_log(self) -> None:
        self.assertIsInstance(self.select({}), LogNotificationProvider)

    def test_half_configured_falls_back_without_crashing(self) -> None:
        with self.assertLogs("notifications.telegram", level="WARNING"):
            only_token = self.select({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN})
        with self.assertLogs("notifications.telegram", level="WARNING"):
            only_chat = self.select({"TELEGRAM_CHAT_ID": FAKE_CHAT_ID})

        self.assertIsInstance(only_token, LogNotificationProvider)
        self.assertIsInstance(only_chat, LogNotificationProvider)

    def test_blank_values_count_as_not_configured(self) -> None:
        provider = self.select({"TELEGRAM_BOT_TOKEN": "   ", "TELEGRAM_CHAT_ID": "  "})

        self.assertIsInstance(provider, LogNotificationProvider)

    def test_an_explicit_provider_wins_over_the_environment(self) -> None:
        explicit = LogNotificationProvider()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": FAKE_CHAT_ID}):
            self.assertIs(notification_provider(explicit), explicit)

    def test_the_factory_reports_absence_rather_than_raising(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(telegram_provider_from_env())


class TelegramDeliverySemanticsTests(unittest.TestCase):
    """At-least-once end to end: a Telegram failure leaves the reminder pending."""

    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.transport = FakeTransport()
        self.provider = TelegramNotificationProvider(
            FAKE_TOKEN, FAKE_CHAT_ID, urlopen=self.transport
        )
        self.service = NotificationService(
            self.conn, self.provider, TZ, due_soon_hours=24, clock=lambda: NOW
        )
        repo.upsert_courses(self.conn, [Course(id=1, shortname="MB", fullname="MATEMATICA BASICA",
                                               category=None, visible=True, progress=None,
                                               startdate=None, enddate=None)])
        due = int((NOW + timedelta(hours=10)).timestamp())
        repo.upsert_assignments(self.conn, [Assignment(id=1, course_id=1, name="Proyecto Final",
                                                       duedate=due, allowsubmissionsfromdate=None,
                                                       cutoffdate=None, grade=None)])
        repo.upsert_assignment_submission_status(self.conn, AssignmentSubmissionStatus(
            assignment_id=1, submission_status="new", grading_status=None,
            cansubmit=True, submitted_at=None))
        self.sync_started = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()

    def tearDown(self) -> None:
        self.conn.close()

    def test_a_delivered_reminder_is_recorded_and_not_sent_twice(self) -> None:
        first = self.service.send_pending(self.sync_started)
        second = self.service.send_pending(self.sync_started)

        self.assertEqual((first.sent, first.failed), (1, 0))
        self.assertEqual((second.pending, second.sent), (0, 0))
        self.assertEqual(len(self.transport.requests), 1)
        self.assertIn("Proyecto Final", self.transport.payload()["text"])

    def test_a_telegram_failure_is_not_recorded_as_sent_and_is_retried(self) -> None:
        failing = TelegramNotificationProvider(
            FAKE_TOKEN, FAKE_CHAT_ID, urlopen=FakeTransport(error=http_error(502, b"{}"))
        )
        service = NotificationService(self.conn, failing, TZ, due_soon_hours=24, clock=lambda: NOW)

        failed = service.send_pending(self.sync_started)
        retried = self.service.send_pending(self.sync_started)  # Telegram is back

        self.assertEqual((failed.sent, failed.failed), (0, 1))
        self.assertIn("TelegramDeliveryError", failed.first_error)
        self.assertEqual(retried.sent, 1)

    def test_the_failure_detail_kept_for_the_operator_carries_no_token(self) -> None:
        service = NotificationService(
            self.conn,
            TelegramNotificationProvider(
                FAKE_TOKEN, FAKE_CHAT_ID,
                urlopen=FakeTransport(error=http_error(401, b'{"ok":false,"description":"Unauthorized"}')),
            ),
            TZ, due_soon_hours=24, clock=lambda: NOW,
        )

        with self.assertLogs("notifications.service", level="WARNING") as logs:
            result = service.send_pending(self.sync_started)

        self.assertNotIn(FAKE_TOKEN, result.first_error)
        self.assertIn("401", result.first_error)
        # The same error is logged for the operator: still no token in it.
        self.assertNotIn(FAKE_TOKEN, "\n".join(logs.output))


class SchedulerIsolationTests(unittest.TestCase):
    """Telegram being down is never allowed to stop the scheduler."""

    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.time = FakeTime(NOW)
        self.jobs = {name: FakeJob(name, time=self.time) for name in PIPELINE}
        provider = TelegramNotificationProvider(
            FAKE_TOKEN, FAKE_CHAT_ID, urlopen=FakeTransport(error=urllib.error.URLError("down"))
        )
        self.service = NotificationService(
            self.conn, provider, TZ, due_soon_hours=24, clock=lambda: NOW
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_a_failing_telegram_does_not_stop_the_scheduler(self) -> None:
        # The job fails every attempt, so the scheduler tries to send the
        # operational alert - through a Telegram that is unreachable.
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("boom")] * MAX_ATTEMPTS
        scheduler = Scheduler(
            self.conn, self.jobs, make_config(retry_delay_seconds=0),
            on_job_failed=self.service.notify_job_failure,
            clock=self.time.clock, sleep=self.time.sleep, monotonic=self.time.monotonic,
        )
        scheduler.request(MOODLE_SYNC, TRIGGER_MANUAL)

        while scheduler.step():  # must not raise
            pass

        last = history.last_execution(self.conn, MOODLE_SYNC)
        self.assertEqual(last.status, history.STATUS_FAILED)
        # Still usable afterwards: the next job runs normally.
        scheduler.request(INGESTION, TRIGGER_MANUAL)
        while scheduler.step():
            pass
        self.assertEqual(history.last_execution(self.conn, INGESTION).status, history.STATUS_DONE)


if __name__ == "__main__":
    unittest.main()

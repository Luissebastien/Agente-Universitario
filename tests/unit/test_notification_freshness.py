"""Reminders must not go out for work that has already been handed in.

Reminders run hourly; moodle_sync, which refreshes the stored submission
status, runs every six hours. Between the two, the status can be stale long
enough to remind someone about an assignment they submitted hours ago - the
defect these tests pin down. NotificationJob closes the gap by re-reading the
status of the few assignments it is about to notify.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from database import moodle_repository as moodle_repo
from database import scheduler_repository as history
from database.db import connect
from moodle.exceptions import MoodleConnectionError
from moodle.models import Assignment, AssignmentSubmissionStatus, Course
from notifications.providers import NotificationProvider
from notifications.reminders import ReminderRule
from notifications.service import NotificationService
from scheduler.jobs import TRIGGER_INTERVAL, NotificationJob, RunContext

TZ = ZoneInfo("America/Santo_Domingo")
NOW = datetime(2026, 10, 11, 23, 0, tzinfo=TZ)
RULES = (
    ReminderRule(id="1h", offset_seconds=3600,
                 title_template="Ultima hora: {assignment}", body_template="{course}"),
)


class Capture(NotificationProvider):
    name = "capture"

    def __init__(self) -> None:
        self.sent = []

    def send(self, notification) -> None:
        self.sent.append(notification)


class _FakeClientFactory:
    """Stands in for MoodleClient.from_env, recording what it was asked.

    `calls` is what proves the cost argument: Moodle is only consulted about
    assignments that are about to be reminded, so an ordinary run asks nothing.
    """

    def __init__(self, *, submitted: bool, fails: bool, group: bool = False) -> None:
        self.submitted = submitted
        self.fails = fails
        self.group = group
        self.calls: list[int] = []
        self.factory_calls = 0

    def __call__(self) -> "_FakeClientFactory":
        self.factory_calls += 1
        return self

    def __enter__(self) -> "_FakeClientFactory":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def call(self, function_name: str, params: dict | None = None):
        if self.fails:
            raise MoodleConnectionError("site unreachable")
        self.calls.append(params["assignid"])
        # Moodle's real shape: the status is nested under lastattempt, and a
        # group assignment reports it as 'teamsubmission' rather than
        # 'submission' (see AssignmentSubmissionStatus.from_moodle).
        key = "teamsubmission" if self.group else "submission"
        return {
            "lastattempt": {
                "cansubmit": not self.submitted,
                "gradingstatus": "notgraded",
                key: {"status": "submitted" if self.submitted else "new",
                      "timemodified": 1760000000},
            }
        }


class SubmissionFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.provider = Capture()
        self.service = NotificationService(
            self.conn, self.provider, TZ, lambda: RULES, clock=lambda: NOW
        )
        moodle_repo.upsert_courses(self.conn, [Course(
            id=1, shortname="SW", fullname="Ingenieria de Software", category=None,
            visible=True, progress=None, startdate=None, enddate=None)])
        moodle_repo.upsert_assignments(self.conn, [Assignment(
            id=77, course_id=1, name="Proyecto en grupo",
            duedate=int((NOW + timedelta(minutes=59)).timestamp()),
            allowsubmissionsfromdate=None, cutoffdate=None, grade=None)])
        # What the last sync stored, six hours ago: not yet submitted.
        self.store_status("new")
        # A successful sync, so confirmed_since lets the assignment through.
        execution_id = history.start_execution(
            self.conn, "moodle_sync", TRIGGER_INTERVAL, 1,
            (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat())
        history.finish_execution(self.conn, execution_id, history.STATUS_DONE,
                                 datetime.now(timezone.utc).isoformat(), 1.0)

    def tearDown(self) -> None:
        self.conn.close()

    def store_status(self, status: str) -> None:
        moodle_repo.upsert_assignment_submission_status(self.conn, AssignmentSubmissionStatus(
            assignment_id=77, submission_status=status, grading_status=None,
            cansubmit=status != "submitted", submitted_at=None))

    def client_factory(self, *, submitted: bool = False, fails: bool = False,
                       group: bool = False):
        """A Moodle that reports the status the student actually has now."""
        return _FakeClientFactory(submitted=submitted, fails=fails, group=group)

    def run_job(self, factory=None) -> None:
        job = NotificationJob(self.conn, self.service, factory)
        job.run(RunContext(trigger=TRIGGER_INTERVAL, should_stop=lambda: False))

    # ---- the reported defect -------------------------------------------

    def test_a_reminder_is_not_sent_for_work_submitted_since_the_last_sync(self) -> None:
        # The stored status still says "new"; Moodle says it was submitted.
        self.run_job(self.client_factory(submitted=True))

        self.assertEqual(self.provider.sent, [])

    def test_without_the_refresh_the_stale_reminder_would_go_out(self) -> None:
        # Same fixture, no Moodle client: this is the behaviour being fixed,
        # kept as a test so the difference is visible rather than asserted.
        self.run_job(None)

        self.assertEqual([n.notification_type for n in self.provider.sent],
                         ["assignment_due_1h"])

    def test_a_reminder_still_goes_out_when_the_work_is_really_pending(self) -> None:
        self.run_job(self.client_factory(submitted=False))

        self.assertEqual([n.notification_type for n in self.provider.sent],
                         ["assignment_due_1h"])

    def test_a_group_submission_also_silences_the_reminder(self) -> None:
        # The reported case was group work, which Moodle reports under
        # 'teamsubmission' instead of 'submission'.
        self.run_job(self.client_factory(submitted=True, group=True))

        self.assertEqual(self.provider.sent, [])

    # ---- cost and failure ----------------------------------------------

    def test_moodle_is_only_asked_about_what_is_about_to_be_notified(self) -> None:
        factory = self.client_factory(submitted=False)
        self.run_job(factory)

        self.assertEqual(factory.calls, [77])

    def test_nothing_pending_means_moodle_is_never_called(self) -> None:
        self.store_status("submitted")
        factory = self.client_factory(submitted=True)

        self.run_job(factory)

        self.assertEqual(factory.factory_calls, 0)
        self.assertEqual(self.provider.sent, [])

    def test_a_moodle_outage_does_not_silence_the_reminder(self) -> None:
        # Falling back to the stored status is deliberate: a redundant
        # reminder is a smaller harm than a deadline nobody mentions.
        with self.assertLogs("scheduler.jobs", level="WARNING") as logs:
            self.run_job(self.client_factory(fails=True))

        self.assertEqual([n.notification_type for n in self.provider.sent],
                         ["assignment_due_1h"])
        self.assertIn("using the stored status", "\n".join(logs.output))

    # ---- evidence that it worked ---------------------------------------

    def test_a_dropped_reminder_is_recorded_in_the_execution_detail(self) -> None:
        # Without this, a reminder correctly withheld looks exactly like a run
        # that never had anything to send - and journald rotates, while this
        # detail is append-only and goes into the backup.
        job = NotificationJob(self.conn, self.service, self.client_factory(submitted=True))

        result = job.run(RunContext(trigger=TRIGGER_INTERVAL, should_stop=lambda: False))

        self.assertIn("1 dropped", result.detail)
        self.assertIn("submitted since the last sync", result.detail)

    def test_an_ordinary_run_says_nothing_about_dropped_reminders(self) -> None:
        job = NotificationJob(self.conn, self.service, self.client_factory(submitted=False))

        result = job.run(RunContext(trigger=TRIGGER_INTERVAL, should_stop=lambda: False))

        self.assertEqual(result.detail, "1 sent, 0 failed")

    def test_the_refresh_itself_is_logged_so_it_can_be_seen_working(self) -> None:
        with self.assertLogs("scheduler.jobs", level="INFO") as logs:
            self.run_job(self.client_factory(submitted=True))

        output = "\n".join(logs.output)
        self.assertIn("Re-read submission status of 1 assignment(s)", output)
        self.assertIn("already been handed in", output)
        # Counts only: no course or assignment name reaches the log.
        self.assertNotIn("Proyecto", output)

    def test_the_refreshed_status_is_persisted_for_the_next_run(self) -> None:
        self.run_job(self.client_factory(submitted=True))

        row = self.conn.execute(
            "SELECT submission_status FROM assignment_submission_status WHERE assignment_id = 77"
        ).fetchone()
        self.assertEqual(row["submission_status"], "submitted")


if __name__ == "__main__":
    unittest.main()

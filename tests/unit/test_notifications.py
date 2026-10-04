import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from database import moodle_repository as repo
from database.db import connect
from moodle.models import Assignment, AssignmentSubmissionStatus, Course
from notifications.models import NOTIFICATION_ASSIGNMENT_DUE_SOON, NOTIFICATION_JOB_FAILED, Notification
from notifications.providers import NotificationProvider
from notifications.service import NotificationService

TZ = ZoneInfo("America/Santo_Domingo")
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=TZ)
HOUR = 3600


class RecordingProvider(NotificationProvider):
    name = "recording"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Notification] = []
        self.fail = fail

    def send(self, notification: Notification) -> None:
        if self.fail:
            raise ConnectionError("provider down")
        self.sent.append(notification)


class NotificationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.now = NOW
        self.provider = RecordingProvider()
        self.service = NotificationService(self.conn, self.provider, TZ, due_soon_hours=24,
                                           clock=lambda: self.now)
        repo.upsert_courses(self.conn, [Course(id=1, shortname="ED", fullname="MATEMATICA BASICA",
                                               category=None, visible=True, progress=None,
                                               startdate=None, enddate=None)])
        # The "latest successful sync" started just before the assignments were upserted.
        self.sync_started = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()

    def tearDown(self) -> None:
        self.conn.close()

    def add_assignment(self, assignment_id: int, due_in_hours: float, status: str | None = "new") -> None:
        due = int((self.now + timedelta(hours=due_in_hours)).timestamp())
        repo.upsert_assignments(self.conn, [Assignment(id=assignment_id, course_id=1, name=f"Tarea {assignment_id}",
                                                       duedate=due, allowsubmissionsfromdate=None,
                                                       cutoffdate=None, grade=None)])
        if status is not None:
            repo.upsert_assignment_submission_status(self.conn, AssignmentSubmissionStatus(
                assignment_id=assignment_id, submission_status=status, grading_status=None,
                cansubmit=True, submitted_at=None))

    def pending_ids(self) -> list[str]:
        return [n.subject_key for n in self.service.pending(self.sync_started)]

    def test_deterministic_rule_selects_only_due_soon_pending_assignments(self) -> None:
        self.add_assignment(1, due_in_hours=10)                      # due soon, not submitted -> yes
        self.add_assignment(2, due_in_hours=10, status="submitted")  # already submitted
        self.add_assignment(3, due_in_hours=48)                      # too far away
        self.add_assignment(4, due_in_hours=-1)                      # already past due
        self.add_assignment(5, due_in_hours=10, status=None)         # status unknown -> fail closed

        self.assertEqual(self.pending_ids(), ["assignment:1"])

    def test_notification_identity_and_local_time(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        [notification] = self.service.pending(self.sync_started)

        self.assertEqual(notification.notification_type, NOTIFICATION_ASSIGNMENT_DUE_SOON)
        due_utc = (self.now + timedelta(hours=10)).astimezone(timezone.utc)
        self.assertEqual(datetime.fromisoformat(notification.scheduled_for), due_utc)
        self.assertIn("2026-10-02 22:00", notification.body)  # America/Santo_Domingo
        self.assertIn("MATEMATICA BASICA", notification.body)

    def test_each_notification_is_sent_only_once(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        first = self.service.send_pending(self.sync_started)
        second = self.service.send_pending(self.sync_started)

        self.assertEqual((first.sent, second.sent), (1, 0))
        self.assertEqual(len(self.provider.sent), 1)

    def test_a_changed_deadline_is_a_new_notification(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        self.service.send_pending(self.sync_started)
        self.add_assignment(1, due_in_hours=20)  # teacher moved the deadline

        self.assertEqual(self.service.send_pending(self.sync_started).sent, 1)

    def test_unsent_notification_is_retried_next_run(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        self.provider.fail = True
        failed = self.service.send_pending(self.sync_started)
        self.provider.fail = False
        retried = self.service.send_pending(self.sync_started)

        self.assertEqual((failed.sent, failed.failed, retried.sent), (0, 1, 1))

    def test_assignment_not_confirmed_by_the_latest_sync_is_ignored(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        later_sync = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        self.assertEqual(self.service.pending(later_sync), [])

    def test_nothing_is_notified_before_any_successful_sync(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        self.assertEqual(self.service.pending(None), [])

    def test_job_failure_alert_goes_through_the_provider(self) -> None:
        self.service.notify_job_failure("moodle_sync", "MoodleConnectionError: down", NOW)

        [notification] = self.provider.sent
        self.assertEqual(notification.notification_type, NOTIFICATION_JOB_FAILED)
        self.assertIn("moodle_sync", notification.title)
        self.assertIn("MoodleConnectionError: down", notification.body)


if __name__ == "__main__":
    unittest.main()

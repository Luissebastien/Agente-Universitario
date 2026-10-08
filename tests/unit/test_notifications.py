import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from database import moodle_repository as repo
from database.db import connect
from moodle.models import Assignment, AssignmentSubmissionStatus, Course
from notifications.models import NOTIFICATION_JOB_FAILED, Notification
from notifications.providers import NotificationProvider
from notifications.reminders import ReminderRule, applicable_rule, describe_remaining
from notifications.service import NotificationService

TZ = ZoneInfo("America/Santo_Domingo")
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=TZ)
HOUR = 3600

# The MVP set. Templates are distinguishable on purpose, so a test can tell
# which rule produced a message without reading the offsets back.
RULES = (
    ReminderRule(id="24h", offset_seconds=24 * HOUR,
                 title_template="24h: {assignment}",
                 body_template="{course} vence el {due_date} a las {due_time}, quedan {remaining}"),
    ReminderRule(id="12h", offset_seconds=12 * HOUR,
                 title_template="12h: {assignment}", body_template="{course} a las {due_time}"),
    ReminderRule(id="6h", offset_seconds=6 * HOUR,
                 title_template="6h: {assignment}", body_template="{course} a las {due_time}"),
    ReminderRule(id="1h", offset_seconds=1 * HOUR,
                 title_template="1h: {assignment}", body_template="{course} en {remaining}"),
)


class RecordingProvider(NotificationProvider):
    name = "recording"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Notification] = []
        self.fail = fail

    def send(self, notification: Notification) -> None:
        if self.fail:
            raise ConnectionError("provider down")
        self.sent.append(notification)


class BandSelectionTests(unittest.TestCase):
    """applicable_rule alone: which reminder a remaining time falls into."""

    def rule_at(self, remaining_hours: float) -> str | None:
        rule = applicable_rule(RULES, remaining_hours * HOUR)
        return rule.id if rule else None

    def test_each_band_runs_from_the_next_smaller_offset_up_to_its_own(self) -> None:
        self.assertEqual(self.rule_at(24), "24h")      # the boundary belongs to its own rule
        self.assertEqual(self.rule_at(20), "24h")
        self.assertEqual(self.rule_at(12.01), "24h")
        self.assertEqual(self.rule_at(12), "12h")
        self.assertEqual(self.rule_at(8), "12h")
        self.assertEqual(self.rule_at(6), "6h")
        self.assertEqual(self.rule_at(5), "6h")
        self.assertEqual(self.rule_at(1), "1h")
        self.assertEqual(self.rule_at(0.25), "1h")

    def test_a_deadline_further_away_than_every_rule_matches_none(self) -> None:
        self.assertIsNone(self.rule_at(25))
        self.assertIsNone(self.rule_at(500))

    def test_removing_a_rule_widens_the_neighbouring_band(self) -> None:
        without_12h = tuple(r for r in RULES if r.id != "12h")
        self.assertEqual(applicable_rule(without_12h, 8 * HOUR).id, "24h")
        self.assertEqual(applicable_rule(without_12h, 6 * HOUR).id, "6h")

    def test_the_rule_is_the_notification_type(self) -> None:
        self.assertEqual([r.notification_type for r in RULES],
                         ["assignment_due_24h", "assignment_due_12h",
                          "assignment_due_6h", "assignment_due_1h"])


class DescribeRemainingTests(unittest.TestCase):
    def test_reads_as_words_with_singulars_handled(self) -> None:
        self.assertEqual(describe_remaining(30), "menos de un minuto")
        self.assertEqual(describe_remaining(60), "1 minuto")
        self.assertEqual(describe_remaining(25 * 60), "25 minutos")
        self.assertEqual(describe_remaining(HOUR), "1 hora")
        self.assertEqual(describe_remaining(10 * HOUR), "10 horas")
        self.assertEqual(describe_remaining(24 * HOUR), "1 dia")

    def test_time_left_is_never_rounded_up(self) -> None:
        # Telling a student "1 dia" with 23h58m left would give them a day
        # they do not have.
        self.assertEqual(describe_remaining(23.98 * HOUR), "23 horas")
        self.assertEqual(describe_remaining(59.9 * 60), "59 minutos")
        self.assertEqual(describe_remaining(47 * HOUR), "1 dia")


class NotificationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.now = NOW
        self.provider = RecordingProvider()
        self.service = NotificationService(self.conn, self.provider, TZ, lambda: RULES,
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

    def pending_types(self) -> list[str]:
        return [n.notification_type for n in self.service.pending(self.sync_started)]

    def send_now(self) -> list[str]:
        """Run one notification pass and report what was delivered."""
        before = len(self.provider.sent)
        self.service.send_pending(self.sync_started)
        return [n.notification_type for n in self.provider.sent[before:]]

    def advance_to(self, due_in_hours: float, assignment_id: int = 1) -> None:
        """Move the clock so the given assignment is this far from its deadline."""
        due = datetime.fromtimestamp(
            self.conn.execute("SELECT duedate FROM assignments WHERE id = ?",
                              (assignment_id,)).fetchone()["duedate"], TZ)
        self.now = due - timedelta(hours=due_in_hours)

    # ---- which assignments are eligible at all --------------------------

    def test_only_pending_assignments_inside_a_band_are_selected(self) -> None:
        self.add_assignment(1, due_in_hours=10)                      # in the 12h band -> yes
        self.add_assignment(2, due_in_hours=10, status="submitted")  # already submitted
        self.add_assignment(3, due_in_hours=48)                      # beyond every rule
        self.add_assignment(4, due_in_hours=-1)                      # already past due
        self.add_assignment(5, due_in_hours=10, status=None)         # status unknown -> fail closed

        notifications = self.service.pending(self.sync_started)
        self.assertEqual([(n.subject_key, n.notification_type) for n in notifications],
                         [("assignment:1", "assignment_due_12h")])

    def test_assignment_not_confirmed_by_the_latest_sync_is_ignored(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        later_sync = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        self.assertEqual(self.service.pending(later_sync), [])

    def test_nothing_is_notified_before_any_successful_sync(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        self.assertEqual(self.service.pending(None), [])

    # ---- the series ------------------------------------------------------

    def test_a_deadline_produces_every_reminder_once_as_it_approaches(self) -> None:
        self.add_assignment(1, due_in_hours=30)  # not yet in any band

        self.assertEqual(self.send_now(), [])
        delivered = []
        for remaining in (23, 20, 11, 8, 5, 3, 0.5, 0.2):
            self.advance_to(remaining)
            delivered.append((remaining, self.send_now()))

        self.assertEqual(delivered, [
            (23, ["assignment_due_24h"]),   # enters the 24h band
            (20, []),                       # same band, already sent
            (11, ["assignment_due_12h"]),
            (8, []),
            (5, ["assignment_due_6h"]),
            (3, []),
            (0.5, ["assignment_due_1h"]),
            (0.2, []),
        ])

    def test_each_rule_is_sent_at_most_once_for_a_deadline(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        first = self.send_now()
        second = self.send_now()

        self.assertEqual(first, ["assignment_due_12h"])
        self.assertEqual(second, [])

    def test_a_changed_deadline_starts_a_fresh_series(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        self.assertEqual(self.send_now(), ["assignment_due_12h"])

        self.add_assignment(1, due_in_hours=11)  # professor moved it: new identity
        self.assertEqual(self.send_now(), ["assignment_due_12h"])

    def test_submitting_stops_the_rest_of_the_series(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        self.assertEqual(self.send_now(), ["assignment_due_12h"])

        self.add_assignment(1, due_in_hours=10, status="submitted")
        self.advance_to(0.5)
        self.assertEqual(self.send_now(), [])

    # ---- no backfill -----------------------------------------------------

    def test_an_assignment_discovered_late_gets_only_its_current_band(self) -> None:
        # First seen with five hours left: the 24h and 12h windows are gone,
        # and must not arrive all at once now.
        self.add_assignment(1, due_in_hours=5)

        self.assertEqual(self.send_now(), ["assignment_due_6h"])
        self.advance_to(0.5)
        self.assertEqual(self.send_now(), ["assignment_due_1h"])

    def test_bands_passed_while_the_system_was_down_are_not_replayed(self) -> None:
        self.add_assignment(1, due_in_hours=23)
        self.assertEqual(self.send_now(), ["assignment_due_24h"])

        self.advance_to(2)  # nothing ran between 23h and 2h left
        self.assertEqual(self.send_now(), ["assignment_due_6h"])  # only the current band
        self.advance_to(0.5)
        self.assertEqual(self.send_now(), ["assignment_due_1h"])

    # ---- delivery --------------------------------------------------------

    def test_an_unsent_reminder_is_retried_and_not_recorded(self) -> None:
        self.add_assignment(1, due_in_hours=10)
        self.provider.fail = True
        failed = self.service.send_pending(self.sync_started)
        self.provider.fail = False
        retried = self.service.send_pending(self.sync_started)

        self.assertEqual((failed.sent, failed.failed, retried.sent), (0, 1, 1))

    def test_templates_are_rendered_with_the_assignment_course_and_local_time(self) -> None:
        self.add_assignment(1, due_in_hours=20)
        [notification] = self.service.pending(self.sync_started)

        local_due = self.now + timedelta(hours=20)
        self.assertEqual(notification.title, "24h: Tarea 1")
        self.assertEqual(
            notification.body,
            f"MATEMATICA BASICA vence el {local_due:%Y-%m-%d} a las {local_due:%H:%M}, "
            "quedan 20 horas",
        )
        self.assertEqual(notification.scheduled_for,
                         datetime.fromtimestamp(int(local_due.timestamp()), timezone.utc).isoformat())

    def test_a_name_with_braces_is_delivered_as_written(self) -> None:
        repo.upsert_assignments(self.conn, [Assignment(
            id=1, course_id=1, name="Taller {1} de practica",
            duedate=int((self.now + timedelta(hours=10)).timestamp()),
            allowsubmissionsfromdate=None, cutoffdate=None, grade=None)])
        repo.upsert_assignment_submission_status(self.conn, AssignmentSubmissionStatus(
            assignment_id=1, submission_status="new", grading_status=None,
            cansubmit=True, submitted_at=None))

        [notification] = self.service.pending(self.sync_started)
        self.assertIn("Taller {1} de practica", notification.title)

    def test_job_failure_alert_goes_through_the_provider(self) -> None:
        self.service.notify_job_failure("moodle_sync", "MoodleConnectionError: down", NOW)

        [notification] = self.provider.sent
        self.assertEqual(notification.notification_type, NOTIFICATION_JOB_FAILED)
        self.assertIn("moodle_sync", notification.title)
        self.assertIn("MoodleConnectionError: down", notification.body)

    def test_a_service_with_no_rules_notifies_nothing(self) -> None:
        service = NotificationService(self.conn, self.provider, TZ, lambda: (), clock=lambda: self.now)
        self.add_assignment(1, due_in_hours=1)

        self.assertEqual(service.pending(self.sync_started), [])


if __name__ == "__main__":
    unittest.main()

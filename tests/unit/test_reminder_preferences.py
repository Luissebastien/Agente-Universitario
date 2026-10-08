"""Runtime-configurable reminders: the store, and the invariant protecting it.

Nothing writes to this store in production yet. These tests stand in for the
interface that eventually will - a Telegram command, a phone app, a web page -
and pin down the contract each of them will inherit.
"""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from database import moodle_repository as moodle_repo
from database import reminder_repository as repo
from database.db import connect
from moodle.models import Assignment, AssignmentSubmissionStatus, Course
from notifications.providers import NotificationProvider
from notifications.reminders import (
    TEMPLATE_FIELDS,
    ReminderError,
    ReminderRule,
    narrowest_band_seconds,
    validate,
)
from notifications.service import NotificationService

TZ = ZoneInfo("America/Santo_Domingo")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)
HOUR = 3600


def rule(rule_id: str, hours: float, title: str = "t: {assignment}", body: str = "b: {course}") -> ReminderRule:
    return ReminderRule(id=rule_id, offset_seconds=int(hours * HOUR),
                        title_template=title, body_template=body)


CONFIGURED = (rule("24h", 24), rule("12h", 12), rule("6h", 6), rule("1h", 1))


class Capture(NotificationProvider):
    name = "capture"

    def __init__(self) -> None:
        self.sent = []

    def send(self, notification) -> None:
        self.sent.append(notification)


class BandWidthTests(unittest.TestCase):
    def test_the_tightest_band_is_the_smallest_gap_not_the_smallest_offset(self) -> None:
        # 24h and 23h are both far from the deadline, yet the band between
        # them is only one hour wide - which is what a cycle has to fit into.
        self.assertEqual(narrowest_band_seconds((rule("a", 24), rule("b", 23))), HOUR)
        self.assertEqual(narrowest_band_seconds(CONFIGURED), HOUR)
        self.assertEqual(narrowest_band_seconds((rule("only", 6),)), 6 * HOUR)
        self.assertEqual(narrowest_band_seconds(()), 0)


class ValidationTests(unittest.TestCase):
    def test_a_gap_narrower_than_an_hour_is_refused(self) -> None:
        with self.assertRaises(ReminderError) as raised:
            validate((*CONFIGURED, rule("30m", 0.5)))

        message = str(raised.exception)
        self.assertIn("30 minutes", message)
        self.assertIn("at least 1 hour apart", message)  # says what is allowed

    def test_reminders_an_hour_apart_anywhere_are_accepted(self) -> None:
        validate(CONFIGURED)                      # the tightest gap is the 1h band
        validate((rule("a", 24), rule("b", 23)))  # gaps far from the deadline count too

    def test_a_reminder_closer_than_an_hour_to_the_deadline_is_refused(self) -> None:
        with self.assertRaises(ReminderError):
            validate((rule("20m", 1 / 3),))

    def test_colliding_or_impossible_rules_are_refused(self) -> None:
        for rules in (
            (rule("x", 6), rule("x", 2)),       # same id: one dedup identity
            (rule("a", 6), rule("b", 6)),       # same offset: ambiguous band
            (rule("past", -1),),                # not before the deadline at all
        ):
            with self.subTest(rules=rules), self.assertRaises(ReminderError):
                validate(rules)

    def test_no_rules_at_all_is_a_valid_choice(self) -> None:
        validate(())

    def test_a_template_that_could_not_render_is_refused(self) -> None:
        # An interface accepting free text must not be able to store a
        # template that fails halfway through a batch, with some of the
        # messages already delivered.
        for broken, expected in (
            (rule("x", 6, title="{materia}"), "materia"),
            (rule("x", 6, body="{curso} {dia}"), "curso"),
            (rule("x", 6, title="{assignment"), "not a valid template"),
        ):
            with self.subTest(broken=broken), self.assertRaises(ReminderError) as raised:
                validate((broken,))
            self.assertIn(expected, str(raised.exception))

    def test_every_documented_placeholder_is_accepted(self) -> None:
        every = " ".join(f"{{{field}}}" for field in TEMPLATE_FIELDS)
        validate((rule("x", 6, title=every, body=every),))

    def test_the_store_refuses_an_unrenderable_template(self) -> None:
        conn = connect(":memory:")
        try:
            with self.assertRaises(ReminderError):
                repo.replace_rules(conn, (rule("x", 6, title="{materia}"),))
            self.assertFalse(repo.has_saved(conn))
        finally:
            conn.close()


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_an_untouched_store_defers_to_the_configured_rules(self) -> None:
        self.assertIsNone(repo.get_rules(self.conn))
        self.assertEqual(repo.effective_rules(self.conn, CONFIGURED), CONFIGURED)

    def test_stored_rules_take_over_from_the_configured_ones(self) -> None:
        chosen = (rule("48h", 48, title="dos dias: {assignment}"), rule("3h", 3))
        repo.replace_rules(self.conn, chosen)

        effective = repo.effective_rules(self.conn, CONFIGURED)
        self.assertEqual([r.id for r in effective], ["48h", "3h"])
        self.assertEqual(effective[0].title_template, "dos dias: {assignment}")

    def test_turning_every_reminder_off_is_obeyed_not_treated_as_unset(self) -> None:
        # The distinction that matters: "I want none" must not silently fall
        # back to the host's four.
        repo.replace_rules(self.conn, (),
                           disabled=(rule("24h", 24),))

        self.assertEqual(repo.get_rules(self.conn), ())
        self.assertEqual(repo.effective_rules(self.conn, CONFIGURED), ())

    def test_muting_everything_is_not_the_same_as_never_having_chosen(self) -> None:
        # Regression: both used to be an empty table, so muting every reminder
        # silently restored the host's four.
        self.assertFalse(repo.has_saved(self.conn))

        repo.replace_rules(self.conn, ())

        self.assertTrue(repo.has_saved(self.conn))
        self.assertEqual(repo.get_rules(self.conn), ())
        self.assertEqual(repo.effective_rules(self.conn, CONFIGURED), ())

        repo.clear_rules(self.conn)
        self.assertFalse(repo.has_saved(self.conn))
        self.assertEqual(repo.effective_rules(self.conn, CONFIGURED), CONFIGURED)

    def test_a_disabled_rule_keeps_its_text_for_when_it_comes_back(self) -> None:
        written = rule("24h", 24, title="mi texto propio: {assignment}")
        repo.replace_rules(self.conn, (), disabled=(written,))
        self.assertEqual(repo.get_rules(self.conn), ())

        restored = self.conn.execute(
            "SELECT title_template FROM reminder_preferences WHERE id = '24h'"
        ).fetchone()
        self.assertEqual(restored["title_template"], "mi texto propio: {assignment}")

    def test_replacing_is_a_whole_set_not_a_merge(self) -> None:
        repo.replace_rules(self.conn, CONFIGURED)
        repo.replace_rules(self.conn, (rule("2h", 2),))

        self.assertEqual([r.id for r in repo.get_rules(self.conn)], ["2h"])

    def test_clearing_goes_back_to_the_configured_rules(self) -> None:
        repo.replace_rules(self.conn, (rule("2h", 2),))
        repo.clear_rules(self.conn)

        self.assertIsNone(repo.get_rules(self.conn))
        self.assertEqual(repo.effective_rules(self.conn, CONFIGURED), CONFIGURED)

    def test_an_undeliverable_set_is_refused_and_nothing_is_written(self) -> None:
        repo.replace_rules(self.conn, CONFIGURED)

        with self.assertRaises(ReminderError):
            repo.replace_rules(self.conn, (rule("30m", 0.5),))

        # The previous set survives intact: a rejected save changes nothing.
        self.assertEqual([r.id for r in repo.get_rules(self.conn)],
                         ["24h", "12h", "6h", "1h"])


class TakesEffectWithoutRestartTests(unittest.TestCase):
    """The point of resolving rules per cycle rather than at construction."""

    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.now = NOW
        self.provider = Capture()
        self.service = NotificationService(
            self.conn, self.provider, TZ,
            lambda: repo.effective_rules(self.conn, CONFIGURED),
            clock=lambda: self.now,
        )
        moodle_repo.upsert_courses(self.conn, [Course(
            id=1, shortname="SW", fullname="Ingenieria de Software", category=None,
            visible=True, progress=None, startdate=None, enddate=None)])
        moodle_repo.upsert_assignments(self.conn, [Assignment(
            id=1, course_id=1, name="Proyecto",
            duedate=int((NOW + timedelta(hours=10)).timestamp()),
            allowsubmissionsfromdate=None, cutoffdate=None, grade=None)])
        moodle_repo.upsert_assignment_submission_status(self.conn, AssignmentSubmissionStatus(
            assignment_id=1, submission_status="new", grading_status=None,
            cansubmit=True, submitted_at=None))
        self.confirmed = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()

    def tearDown(self) -> None:
        self.conn.close()

    def types(self) -> list[str]:
        return [n.notification_type for n in self.service.pending(self.confirmed)]

    def test_rules_saved_after_the_service_was_built_are_used_immediately(self) -> None:
        self.assertEqual(self.types(), ["assignment_due_12h"])  # the configured set

        # Same long-lived service object, as in the running daemon.
        repo.replace_rules(self.conn, (rule("10h", 10, title="mio: {assignment}"),))

        self.assertEqual(self.types(), ["assignment_due_10h"])
        self.assertEqual(self.service.pending(self.confirmed)[0].title, "mio: Proyecto")

    def test_turning_everything_off_silences_the_running_service(self) -> None:
        repo.replace_rules(self.conn, ())
        self.assertEqual(self.types(), [])

    def test_clearing_restores_the_configured_rules_without_a_restart(self) -> None:
        repo.replace_rules(self.conn, (rule("10h", 10),))
        repo.clear_rules(self.conn)

        self.assertEqual(self.types(), ["assignment_due_12h"])


if __name__ == "__main__":
    unittest.main()

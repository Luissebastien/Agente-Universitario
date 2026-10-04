import unittest

from database import moodle_repository as repo
from database.db import connect
from moodle.exceptions import MoodleAPIError, MoodleAuthenticationError, MoodleConnectionError
from moodle.models import Assignment, Course
from moodle.sync import MoodleSync

from .test_moodle_sync import (
    RAW_ASSIGNMENTS_RESPONSE,
    RAW_CALENDAR_RESPONSE,
    RAW_GRADE_ITEMS_RESPONSE,
    RAW_SUBMISSION_STATUS_RESPONSE,
    make_client,
)

NOW = 1_790_000_000
NO_UPDATES = {"instances": [], "warnings": []}
SOME_UPDATES = {"instances": [{"contextlevel": "module", "id": 5001,
                               "updates": [{"name": "configuration", "timeupdated": 1}]}],
                "warnings": []}


def contents_for(course_id: int) -> list[dict]:
    # Distinct section/module ids per course (the shared fixture reuses ids,
    # which would move one course's rows to the other on upsert).
    return [{"id": course_id * 10, "section": 0, "name": "General", "summary": "", "visible": 1,
             "modules": [{"id": course_id * 100, "modname": "resource", "name": "Programa",
                          "url": None, "visible": 1,
                          "contents": [{"type": "file", "filename": "p.txt", "filepath": "/",
                                        "filesize": 10, "mimetype": "text/plain",
                                        "fileurl": f"https://campusvirtual.example.edu/webservice/"
                                                   f"pluginfile.php/{course_id}/p.txt",
                                        "timemodified": 111}]}]}]


def client_for_cycle(updates_by_course: dict | None = None):
    client = make_client()
    client.get_course_contents.side_effect = contents_for

    def call(function, params=None):
        return {
            "mod_assign_get_assignments": RAW_ASSIGNMENTS_RESPONSE,
            "mod_assign_get_submission_status": RAW_SUBMISSION_STATUS_RESPONSE,
            "gradereport_user_get_grade_items": RAW_GRADE_ITEMS_RESPONSE,
            "core_calendar_get_action_events_by_courses": RAW_CALENDAR_RESPONSE,
        }[function]

    client.call.side_effect = call
    client.get_enrolled_courses_by_timeline_classification.return_value = []
    updates_by_course = updates_by_course or {}
    client.get_updates_since.side_effect = lambda course_id, since: updates_by_course.get(course_id, NO_UPDATES)
    return client


def calls(client, function):
    return [c for c in client.call.call_args_list if c.args[0] == function]


class SyncChangesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_full_sync_fetches_every_course_without_asking_for_updates(self) -> None:
        client = client_for_cycle()
        report = MoodleSync(client, self.conn).sync_changes(since=None, now=NOW)

        self.assertTrue(report.full)
        self.assertEqual(sorted(report.changed_courses), [101, 102])
        client.get_updates_since.assert_not_called()
        self.assertEqual(client.get_course_contents.call_count, 2)
        self.assertEqual(report.statuses_refreshed, 1)
        self.assertEqual(len(calls(client, "gradereport_user_get_grade_items")), 2)

    def test_incremental_sync_with_no_updates_refetches_nothing_heavy(self) -> None:
        sync = MoodleSync(client_for_cycle(), self.conn)
        sync.sync_changes(since=None, now=NOW)
        repo.upsert_assignment_submission_status  # status now known
        client = client_for_cycle()
        report = MoodleSync(client, self.conn).sync_changes(since=NOW - 3600, now=NOW + 10**8)

        self.assertFalse(report.full)
        self.assertEqual(report.changed_courses, ())
        self.assertEqual(client.get_updates_since.call_count, 2)
        client.get_course_contents.assert_not_called()
        self.assertEqual(calls(client, "gradereport_user_get_grade_items"), [])
        # Calendar and assignment definitions are not covered by the updates API: always bulk-synced.
        self.assertEqual(len(calls(client, "core_calendar_get_action_events_by_courses")), 1)
        self.assertEqual(len(calls(client, "mod_assign_get_assignments")), 1)

    def test_only_courses_with_updates_are_refetched(self) -> None:
        MoodleSync(client_for_cycle(), self.conn).sync_changes(since=None, now=NOW)
        client = client_for_cycle({101: SOME_UPDATES})
        report = MoodleSync(client, self.conn).sync_changes(since=NOW - 3600, now=NOW + 10**8)

        self.assertEqual(report.changed_courses, (101,))
        client.get_course_contents.assert_called_once_with(101)
        client.get_updates_since.assert_any_call(101, NOW - 3600)

    def test_a_detection_warning_counts_as_changed(self) -> None:
        MoodleSync(client_for_cycle(), self.conn).sync_changes(since=None, now=NOW)
        warning = {"instances": [], "warnings": [{"item": "module", "itemid": 1,
                                                  "warningcode": "missingcallback", "message": "x"}]}
        report = MoodleSync(client_for_cycle({102: warning}), self.conn).sync_changes(
            since=NOW - 3600, now=NOW + 10**8)

        self.assertEqual(report.changed_courses, (102,))
        self.assertEqual(report.detection_warnings, 1)

    def test_course_never_content_synced_is_fully_synced(self) -> None:
        client = client_for_cycle()
        report = MoodleSync(client, self.conn).sync_changes(since=NOW - 3600, now=NOW)

        self.assertEqual(sorted(report.changed_courses), [101, 102])
        client.get_updates_since.assert_not_called()

    def test_open_unsubmitted_assignment_status_is_refreshed_every_cycle(self) -> None:
        # Due in the future and still 'new': a teammate could submit the
        # group work without the updates API flagging the course.
        before_due = 1_786_000_000  # fixture assignment is due at 1_786_852_500
        MoodleSync(client_for_cycle(), self.conn).sync_changes(since=None, now=before_due)
        client = client_for_cycle()
        report = MoodleSync(client, self.conn).sync_changes(since=before_due - 3600, now=before_due)

        self.assertEqual(report.changed_courses, ())
        self.assertEqual(report.statuses_refreshed, 1)

        # Once past its due date it is no longer re-queried without a change.
        client = client_for_cycle()
        report = MoodleSync(client, self.conn).sync_changes(since=NOW - 3600, now=NOW)
        self.assertEqual(report.statuses_refreshed, 0)

    def test_assignments_moodle_no_longer_returns_are_not_queried(self) -> None:
        # A deleted assignment (or dropped course) keeps its row forever; its
        # status call would fail every cycle and block the whole pipeline.
        repo.upsert_courses(self.conn, [Course(id=999, shortname="old", fullname="Old", category=None,
                                               visible=True, progress=None, startdate=None, enddate=None)])
        repo.upsert_assignments(self.conn, [Assignment(id=777, course_id=999, name="gone", duedate=NOW + 99,
                                                       allowsubmissionsfromdate=None, cutoffdate=None, grade=None)])
        client = client_for_cycle()
        MoodleSync(client, self.conn).sync_changes(since=None, now=NOW)

        queried = [c.args[1]["assignid"] for c in calls(client, "mod_assign_get_submission_status")]
        self.assertNotIn(777, queried)
        course_ids = calls(client, "mod_assign_get_assignments")[0].args[1]["courseids"]
        self.assertNotIn(999, course_ids)

    def test_api_error_on_one_status_is_an_item_warning(self) -> None:
        client = client_for_cycle()
        base = client.call.side_effect

        def call(function, params=None):
            if function == "mod_assign_get_submission_status":
                raise MoodleAPIError("nopermissions", "no")
            return base(function, params)

        client.call.side_effect = call
        report = MoodleSync(client, self.conn).sync_changes(since=None, now=NOW)

        self.assertEqual(report.statuses_refreshed, 0)
        self.assertEqual(len(report.item_warnings), 1)
        self.assertIn("104926", report.item_warnings[0])

    def test_systemic_errors_fail_the_whole_run(self) -> None:
        for error in (MoodleAuthenticationError("invalidtoken", "x"), MoodleConnectionError("down")):
            with self.subTest(error=type(error).__name__):
                client = client_for_cycle()
                base = client.call.side_effect

                def call(function, params=None, error=error):
                    if function == "mod_assign_get_submission_status":
                        raise error
                    return base(function, params)

                client.call.side_effect = call
                with self.assertRaises(type(error)):
                    MoodleSync(client, connect(":memory:")).sync_changes(since=None, now=NOW)

    def test_updates_api_error_fails_the_run(self) -> None:
        MoodleSync(client_for_cycle(), self.conn).sync_changes(since=None, now=NOW)
        client = client_for_cycle()
        client.get_updates_since.side_effect = MoodleAPIError("invalidrecord", "x")
        with self.assertRaises(MoodleAPIError):
            MoodleSync(client, self.conn).sync_changes(since=NOW - 3600, now=NOW)


if __name__ == "__main__":
    unittest.main()

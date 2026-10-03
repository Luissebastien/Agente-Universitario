import unittest
from unittest.mock import MagicMock

from database.db import connect
from database import moodle_repository as repo
from moodle.client import MoodleClient
from moodle.exceptions import MoodleAPIError
from moodle.models import MoodleUser
from moodle.sync import MoodleSync

SITE_INFO = {"userid": 9001, "username": "10000001", "fullname": "ESTUDIANTE DE PRUEBA"}

RAW_COURSES = [
    {"id": 101, "shortname": "MAT-MAT101-01", "fullname": "MATEMATICA BASICA",
     "category": 308, "visible": 1, "progress": 0, "startdate": 1, "enddate": 2},
    {"id": 102, "shortname": "ING-PRG101-02", "fullname": "PROGRAMACION I",
     "category": 310, "visible": 1, "progress": 40.5, "startdate": 1, "enddate": 2},
]

RAW_CONTENTS = [
    {
        "id": 501,
        "section": 1,
        "name": "Semana 1",
        "summary": "",
        "visible": 1,
        "modules": [
            {
                "id": 5001,
                "modname": "resource",
                "name": "Programa",
                "url": "https://campusvirtual.example.edu/mod/resource/view.php?id=5001",
                "visible": 1,
                "contents": [
                    {
                        "type": "file",
                        "filename": "programa.pdf",
                        "filepath": "/",
                        "filesize": 1000,
                        "mimetype": "application/pdf",
                        "fileurl": "https://campusvirtual.example.edu/webservice/pluginfile.php/1/x.pdf",
                        "timemodified": 111,
                    },
                    {
                        # An embedded external link, not a real file: must be skipped.
                        "type": "url",
                        "fileurl": "https://example.com/not-a-file",
                    },
                ],
            },
            {
                "id": 905634,
                "modname": "label",
                "name": "Texto informativo",
                "url": None,
                "visible": 1,
                "contents": [],
            },
        ],
    }
]


RAW_ASSIGNMENTS_RESPONSE = {
    "courses": [
        {
            "id": 101,
            "assignments": [
                {"id": 104926, "name": "Tarea #1 de ED", "duedate": 1786852500,
                 "allowsubmissionsfromdate": 1786407000, "cutoffdate": 0, "grade": 10},
            ],
        },
        {"id": 102, "assignments": []},
    ],
}

RAW_SUBMISSION_STATUS_RESPONSE = {
    "lastattempt": {
        "gradingstatus": "notgraded",
        "cansubmit": False,
        "submission": {"status": "new", "timemodified": 1786795183},
    },
}

RAW_GRADE_ITEMS_RESPONSE = {
    "usergrades": [
        {
            "courseid": 101,
            "userid": 9001,
            "gradeitems": [
                {"id": 6001, "cmid": 5001, "itemname": "Tarea #1 de ED", "itemtype": "mod",
                 "itemmodule": "assign", "graderaw": 0, "gradeformatted": "0,00",
                 "percentageformatted": "0,00 %"},
                {"id": 6002, "itemtype": "course", "graderaw": None,
                 "gradeformatted": "-", "percentageformatted": "-"},
            ],
        },
    ],
}

RAW_CALENDAR_RESPONSE = {
    "groupedbycourse": [
        {
            "courseid": 101,
            "events": [
                {"id": 807162, "name": "Vencimiento de Tarea #1 de ED", "description": "<p>x</p>",
                 "eventtype": "due", "modulename": "assign", "instance": 5001,
                 "timestart": 1786852500, "timesort": 1786852500, "timeduration": 0,
                 "course": {"id": 101}},
            ],
        },
        {"courseid": 102, "events": []},
    ],
}


def make_client(**overrides) -> MagicMock:
    client = MagicMock(spec=MoodleClient)
    client.get_site_info.return_value = SITE_INFO
    client.get_courses.return_value = RAW_COURSES
    client.get_course_contents.return_value = RAW_CONTENTS
    for name, value in overrides.items():
        getattr(client, name).return_value = value
    return client


class MoodleSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_sync_profile_persists_user(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)

        user = sync.sync_profile()

        self.assertEqual(user.id, 9001)
        row = self.conn.execute("SELECT * FROM moodle_users WHERE id = 9001").fetchone()
        self.assertIsNotNone(row)

    def test_sync_courses_persists_all_courses(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)

        courses = sync.sync_courses(user_id=9001)

        self.assertEqual(len(courses), 2)
        rows = self.conn.execute("SELECT id FROM courses ORDER BY id").fetchall()
        self.assertEqual([r["id"] for r in rows], [101, 102])
        client.get_courses.assert_called_once_with(9001)

    def test_sync_courses_twice_is_idempotent(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)

        sync.sync_courses(user_id=9001)
        sync.sync_courses(user_id=9001)

        rows = self.conn.execute("SELECT * FROM courses").fetchall()
        self.assertEqual(len(rows), 2)

    def test_sync_course_contents_persists_sections_modules_and_files(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)  # courses must exist first (FK)

        sections = sync.sync_course_contents(101)

        self.assertEqual(len(sections), 1)
        self.assertEqual(
            len(self.conn.execute("SELECT * FROM course_sections").fetchall()), 1
        )
        modules = self.conn.execute("SELECT * FROM course_modules ORDER BY id").fetchall()
        self.assertEqual([m["id"] for m in modules], [5001, 905634])

        files = self.conn.execute("SELECT * FROM course_files").fetchall()
        self.assertEqual(len(files), 1)  # the 'url' content entry must be skipped
        self.assertEqual(files[0]["fileurl"], RAW_CONTENTS[0]["modules"][0]["contents"][0]["fileurl"])

        client.get_course_contents.assert_called_once_with(101)

    def test_sync_course_contents_twice_is_idempotent(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)

        sync.sync_course_contents(101)
        sync.sync_course_contents(101)

        self.assertEqual(len(self.conn.execute("SELECT * FROM course_modules").fetchall()), 2)
        self.assertEqual(len(self.conn.execute("SELECT * FROM course_files").fetchall()), 1)


class SyncCourseClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def _make_classification_client(self, by_classification: dict[str, list[dict]] | None = None) -> MagicMock:
        client = make_client()
        by_classification = by_classification or {
            "inprogress": [{"id": 101}],
            "past": [{"id": 102}],
            "future": [],
        }

        def fake_get(classification):
            self.assertIn(classification, ("inprogress", "past", "future"))
            return by_classification[classification]

        client.get_enrolled_courses_by_timeline_classification.side_effect = fake_get
        return client

    def test_persists_classification_per_course(self) -> None:
        client = self._make_classification_client()
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)  # courses must exist first

        result = sync.sync_course_classification()

        self.assertEqual(result, {101: "inprogress", 102: "past"})
        row_4409 = self.conn.execute("SELECT timeline_classification FROM courses WHERE id = 101").fetchone()
        row_22055 = self.conn.execute("SELECT timeline_classification FROM courses WHERE id = 102").fetchone()
        self.assertEqual(row_4409["timeline_classification"], "inprogress")
        self.assertEqual(row_22055["timeline_classification"], "past")

    def test_future_classification_is_persisted(self) -> None:
        client = self._make_classification_client({
            "inprogress": [], "past": [], "future": [{"id": 101}],
        })
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)

        result = sync.sync_course_classification()

        self.assertEqual(result, {101: "future"})
        row = self.conn.execute("SELECT timeline_classification FROM courses WHERE id = 101").fetchone()
        self.assertEqual(row["timeline_classification"], "future")

    def test_calls_all_three_classifications_exactly_once_each(self) -> None:
        client = self._make_classification_client()
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)

        sync.sync_course_classification()

        calls = client.get_enrolled_courses_by_timeline_classification.call_args_list
        self.assertEqual(len(calls), 3)  # no unnecessary/duplicate Moodle calls
        self.assertEqual({c.args[0] for c in calls}, {"inprogress", "past", "future"})

    def test_course_not_yet_synced_does_not_raise(self) -> None:
        # classification arrives for a course_id we haven't synced via
        # sync_courses() yet - must be a safe no-op, not a crash.
        client = self._make_classification_client()
        sync = MoodleSync(client, self.conn)  # sync_courses() deliberately not called

        result = sync.sync_course_classification()  # should not raise

        self.assertEqual(result, {101: "inprogress", 102: "past"})

    def test_empty_response_for_every_classification(self) -> None:
        client = self._make_classification_client({"inprogress": [], "past": [], "future": []})
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)

        result = sync.sync_course_classification()

        self.assertEqual(result, {})
        row = self.conn.execute("SELECT timeline_classification FROM courses WHERE id = 101").fetchone()
        self.assertIsNone(row["timeline_classification"])  # untouched, not guessed

    def test_running_twice_does_not_duplicate_courses(self) -> None:
        client = self._make_classification_client()
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)

        sync.sync_course_classification()
        sync.sync_course_classification()

        rows = self.conn.execute("SELECT * FROM courses").fetchall()
        self.assertEqual(len(rows), 2)  # still just the 2 original courses

    def test_course_classification_can_change_between_syncs(self) -> None:
        # e.g. a course moves from "future" to "inprogress" over time.
        client = self._make_classification_client({
            "inprogress": [], "past": [], "future": [{"id": 101}],
        })
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)
        sync.sync_course_classification()
        self.assertEqual(
            self.conn.execute("SELECT timeline_classification FROM courses WHERE id = 101").fetchone()[
                "timeline_classification"
            ],
            "future",
        )

        client.get_enrolled_courses_by_timeline_classification.side_effect = lambda c: (
            [{"id": 101}] if c == "inprogress" else []
        )
        sync.sync_course_classification()

        row = self.conn.execute("SELECT timeline_classification FROM courses WHERE id = 101").fetchone()
        self.assertEqual(row["timeline_classification"], "inprogress")

    def test_moodle_error_propagates(self) -> None:
        # Consistent with every other sync method: a Moodle-raised error is
        # never caught/swallowed here, it propagates to the caller.
        client = make_client()
        client.get_enrolled_courses_by_timeline_classification.side_effect = MoodleAPIError(
            "someerror", "boom"
        )
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)

        with self.assertRaises(MoodleAPIError):
            sync.sync_course_classification()


class SyncAssignmentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_sync_assignments_persists_only_nonempty_courses(self) -> None:
        client = make_client(call=RAW_ASSIGNMENTS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)

        assignments = sync.sync_assignments([101, 102])

        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].id, 104926)
        client.call.assert_called_once_with(
            "mod_assign_get_assignments", {"courseids": [101, 102]}
        )

    def test_sync_assignments_defaults_course_ids_from_repository(self) -> None:
        client = make_client(call=RAW_ASSIGNMENTS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)  # persists courses 101 and 102

        sync.sync_assignments()

        called_args = client.call.call_args[0][1]
        self.assertEqual(sorted(called_args["courseids"]), [101, 102])

    def test_sync_assignments_twice_is_idempotent(self) -> None:
        client = make_client(call=RAW_ASSIGNMENTS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)

        sync.sync_assignments([101])
        sync.sync_assignments([101])

        rows = self.conn.execute("SELECT * FROM assignments").fetchall()
        self.assertEqual(len(rows), 1)

    def test_sync_assignments_with_no_courses_does_nothing(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)

        result = sync.sync_assignments([])

        self.assertEqual(result, [])
        client.call.assert_not_called()


class SyncAssignmentSubmissionStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        repo_setup_client = make_client(call=RAW_ASSIGNMENTS_RESPONSE)
        self.sync = MoodleSync(repo_setup_client, self.conn)
        self.sync.sync_courses(user_id=9001)
        self.sync.sync_assignments([101])

    def tearDown(self) -> None:
        self.conn.close()

    def test_sync_submission_status_persists_and_is_idempotent(self) -> None:
        self.sync._client.call.return_value = RAW_SUBMISSION_STATUS_RESPONSE

        status = self.sync.sync_assignment_submission_status(104926)
        self.sync.sync_assignment_submission_status(104926)

        self.assertEqual(status.submission_status, "new")
        rows = self.conn.execute("SELECT * FROM assignment_submission_status").fetchall()
        self.assertEqual(len(rows), 1)
        self.sync._client.call.assert_called_with(
            "mod_assign_get_submission_status", {"assignid": 104926}
        )


class SyncGradesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_sync_grades_persists_items_and_skips_none_userid_lookup(self) -> None:
        client = make_client(call=RAW_GRADE_ITEMS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        repo.upsert_user(self.conn, MoodleUser(id=9001, username="u", fullname="U"))
        sync.sync_courses(user_id=9001)

        grades = sync.sync_grades(101, user_id=9001)

        self.assertEqual(len(grades), 2)
        client.call.assert_called_once_with(
            "gradereport_user_get_grade_items", {"courseid": 101, "userid": 9001}
        )
        client.get_site_info.assert_not_called()  # user_id was given explicitly

    def test_sync_grades_looks_up_userid_when_not_given(self) -> None:
        client = make_client(call=RAW_GRADE_ITEMS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        repo.upsert_user(self.conn, MoodleUser(id=9001, username="u", fullname="U"))
        sync.sync_courses(user_id=9001)

        sync.sync_grades(101)

        client.get_site_info.assert_called_once()

    def test_sync_grades_twice_is_idempotent(self) -> None:
        client = make_client(call=RAW_GRADE_ITEMS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        repo.upsert_user(self.conn, MoodleUser(id=9001, username="u", fullname="U"))
        sync.sync_courses(user_id=9001)

        sync.sync_grades(101, user_id=9001)
        sync.sync_grades(101, user_id=9001)

        rows = self.conn.execute("SELECT * FROM grades").fetchall()
        self.assertEqual(len(rows), 2)


class SyncCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_sync_calendar_persists_events_across_courses(self) -> None:
        client = make_client(call=RAW_CALENDAR_RESPONSE)
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)

        events = sync.sync_calendar([101, 102])

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, 807162)
        self.assertEqual(events[0].description, "x")

    def test_sync_calendar_twice_is_idempotent(self) -> None:
        client = make_client(call=RAW_CALENDAR_RESPONSE)
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=9001)

        sync.sync_calendar([101])
        sync.sync_calendar([101])

        rows = self.conn.execute("SELECT * FROM calendar_events").fetchall()
        self.assertEqual(len(rows), 1)

    def test_sync_calendar_with_no_courses_does_nothing(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)

        result = sync.sync_calendar([])

        self.assertEqual(result, [])
        client.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()

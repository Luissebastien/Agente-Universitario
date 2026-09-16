import unittest
from unittest.mock import MagicMock

from database.db import connect
from database import moodle_repository as repo
from moodle.client import MoodleClient
from moodle.models import MoodleUser
from moodle.sync import MoodleSync

SITE_INFO = {"userid": 15465, "username": "1130421", "fullname": "LUIS JIMENEZ"}

RAW_COURSES = [
    {"id": 4409, "shortname": "CBA-CBM203-01", "fullname": "ECUACIONES DIFERENCIALES",
     "category": 308, "visible": 1, "progress": 0, "startdate": 1, "enddate": 2},
    {"id": 22055, "shortname": "ING-IDS343-02", "fullname": "ESTRUCTURAS DE DATOS",
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
                "id": 905633,
                "modname": "resource",
                "name": "Programa",
                "url": "https://campusvirtual.intec.edu.do/mod/resource/view.php?id=905633",
                "visible": 1,
                "contents": [
                    {
                        "type": "file",
                        "filename": "programa.pdf",
                        "filepath": "/",
                        "filesize": 1000,
                        "mimetype": "application/pdf",
                        "fileurl": "https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/x.pdf",
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
            "id": 4409,
            "assignments": [
                {"id": 104926, "name": "Tarea #1 de ED", "duedate": 1786852500,
                 "allowsubmissionsfromdate": 1786407000, "cutoffdate": 0, "grade": 10},
            ],
        },
        {"id": 22055, "assignments": []},
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
            "courseid": 4409,
            "userid": 15465,
            "gradeitems": [
                {"id": 185067, "cmid": 905633, "itemname": "Tarea #1 de ED", "itemtype": "mod",
                 "itemmodule": "assign", "graderaw": 0, "gradeformatted": "0,00",
                 "percentageformatted": "0,00 %"},
                {"id": 43466, "itemtype": "course", "graderaw": None,
                 "gradeformatted": "-", "percentageformatted": "-"},
            ],
        },
    ],
}

RAW_CALENDAR_RESPONSE = {
    "groupedbycourse": [
        {
            "courseid": 4409,
            "events": [
                {"id": 807162, "name": "Vencimiento de Tarea #1 de ED", "description": "<p>x</p>",
                 "eventtype": "due", "modulename": "assign", "instance": 905633,
                 "timestart": 1786852500, "timesort": 1786852500, "timeduration": 0,
                 "course": {"id": 4409}},
            ],
        },
        {"courseid": 22055, "events": []},
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

        self.assertEqual(user.id, 15465)
        row = self.conn.execute("SELECT * FROM moodle_users WHERE id = 15465").fetchone()
        self.assertIsNotNone(row)

    def test_sync_courses_persists_all_courses(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)

        courses = sync.sync_courses(user_id=15465)

        self.assertEqual(len(courses), 2)
        rows = self.conn.execute("SELECT id FROM courses ORDER BY id").fetchall()
        self.assertEqual([r["id"] for r in rows], [4409, 22055])
        client.get_courses.assert_called_once_with(15465)

    def test_sync_courses_twice_is_idempotent(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)

        sync.sync_courses(user_id=15465)
        sync.sync_courses(user_id=15465)

        rows = self.conn.execute("SELECT * FROM courses").fetchall()
        self.assertEqual(len(rows), 2)

    def test_sync_course_contents_persists_sections_modules_and_files(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=15465)  # courses must exist first (FK)

        sections = sync.sync_course_contents(4409)

        self.assertEqual(len(sections), 1)
        self.assertEqual(
            len(self.conn.execute("SELECT * FROM course_sections").fetchall()), 1
        )
        modules = self.conn.execute("SELECT * FROM course_modules ORDER BY id").fetchall()
        self.assertEqual([m["id"] for m in modules], [905633, 905634])

        files = self.conn.execute("SELECT * FROM course_files").fetchall()
        self.assertEqual(len(files), 1)  # the 'url' content entry must be skipped
        self.assertEqual(files[0]["fileurl"], RAW_CONTENTS[0]["modules"][0]["contents"][0]["fileurl"])

        client.get_course_contents.assert_called_once_with(4409)

    def test_sync_course_contents_twice_is_idempotent(self) -> None:
        client = make_client()
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=15465)

        sync.sync_course_contents(4409)
        sync.sync_course_contents(4409)

        self.assertEqual(len(self.conn.execute("SELECT * FROM course_modules").fetchall()), 2)
        self.assertEqual(len(self.conn.execute("SELECT * FROM course_files").fetchall()), 1)


class SyncAssignmentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_sync_assignments_persists_only_nonempty_courses(self) -> None:
        client = make_client(call=RAW_ASSIGNMENTS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=15465)

        assignments = sync.sync_assignments([4409, 22055])

        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].id, 104926)
        client.call.assert_called_once_with(
            "mod_assign_get_assignments", {"courseids": [4409, 22055]}
        )

    def test_sync_assignments_defaults_course_ids_from_repository(self) -> None:
        client = make_client(call=RAW_ASSIGNMENTS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=15465)  # persists courses 4409 and 22055

        sync.sync_assignments()

        called_args = client.call.call_args[0][1]
        self.assertEqual(sorted(called_args["courseids"]), [4409, 22055])

    def test_sync_assignments_twice_is_idempotent(self) -> None:
        client = make_client(call=RAW_ASSIGNMENTS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=15465)

        sync.sync_assignments([4409])
        sync.sync_assignments([4409])

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
        self.sync.sync_courses(user_id=15465)
        self.sync.sync_assignments([4409])

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
        repo.upsert_user(self.conn, MoodleUser(id=15465, username="u", fullname="U"))
        sync.sync_courses(user_id=15465)

        grades = sync.sync_grades(4409, user_id=15465)

        self.assertEqual(len(grades), 2)
        client.call.assert_called_once_with(
            "gradereport_user_get_grade_items", {"courseid": 4409, "userid": 15465}
        )
        client.get_site_info.assert_not_called()  # user_id was given explicitly

    def test_sync_grades_looks_up_userid_when_not_given(self) -> None:
        client = make_client(call=RAW_GRADE_ITEMS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        repo.upsert_user(self.conn, MoodleUser(id=15465, username="u", fullname="U"))
        sync.sync_courses(user_id=15465)

        sync.sync_grades(4409)

        client.get_site_info.assert_called_once()

    def test_sync_grades_twice_is_idempotent(self) -> None:
        client = make_client(call=RAW_GRADE_ITEMS_RESPONSE)
        sync = MoodleSync(client, self.conn)
        repo.upsert_user(self.conn, MoodleUser(id=15465, username="u", fullname="U"))
        sync.sync_courses(user_id=15465)

        sync.sync_grades(4409, user_id=15465)
        sync.sync_grades(4409, user_id=15465)

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
        sync.sync_courses(user_id=15465)

        events = sync.sync_calendar([4409, 22055])

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, 807162)
        self.assertEqual(events[0].description, "x")

    def test_sync_calendar_twice_is_idempotent(self) -> None:
        client = make_client(call=RAW_CALENDAR_RESPONSE)
        sync = MoodleSync(client, self.conn)
        sync.sync_courses(user_id=15465)

        sync.sync_calendar([4409])
        sync.sync_calendar([4409])

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

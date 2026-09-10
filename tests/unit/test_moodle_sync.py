import unittest
from unittest.mock import MagicMock

from database.db import connect
from moodle.client import MoodleClient
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


if __name__ == "__main__":
    unittest.main()

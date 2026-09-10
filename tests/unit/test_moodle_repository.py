import unittest

from database.db import connect
from database import moodle_repository as repo
from moodle.models import Course, CourseFile, CourseModule, CourseSection, MoodleUser


class RepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()


class UpsertUserTests(RepositoryTestCase):
    def test_insert_then_update_is_idempotent(self) -> None:
        user = MoodleUser(id=15465, username="1130421", fullname="LUIS JIMENEZ")
        repo.upsert_user(self.conn, user)
        repo.upsert_user(self.conn, user)

        rows = self.conn.execute("SELECT * FROM moodle_users").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fullname"], "LUIS JIMENEZ")

    def test_update_changes_fields_but_keeps_created_at(self) -> None:
        repo.upsert_user(self.conn, MoodleUser(id=1, username="a", fullname="Old Name"))
        first_created_at = self.conn.execute(
            "SELECT created_at FROM moodle_users WHERE id = 1"
        ).fetchone()["created_at"]

        repo.upsert_user(self.conn, MoodleUser(id=1, username="a", fullname="New Name"))
        row = self.conn.execute("SELECT * FROM moodle_users WHERE id = 1").fetchone()

        self.assertEqual(row["fullname"], "New Name")
        self.assertEqual(row["created_at"], first_created_at)


class UpsertCoursesTests(RepositoryTestCase):
    def test_running_sync_twice_does_not_duplicate(self) -> None:
        course = Course(
            id=22055, shortname="IDS343", fullname="Estructuras de Datos", category=310,
            visible=True, progress=0.0, startdate=1, enddate=2,
        )
        repo.upsert_courses(self.conn, [course])
        repo.upsert_courses(self.conn, [course])

        rows = repo.get_courses(self.conn)
        self.assertEqual(len(rows), 1)

    def test_progress_update_is_reflected(self) -> None:
        course = Course(
            id=1, shortname="a", fullname="A", category=None, visible=True,
            progress=0.0, startdate=None, enddate=None,
        )
        repo.upsert_courses(self.conn, [course])

        updated = Course(
            id=1, shortname="a", fullname="A", category=None, visible=True,
            progress=50.0, startdate=None, enddate=None,
        )
        repo.upsert_courses(self.conn, [updated])

        row = self.conn.execute("SELECT progress FROM courses WHERE id = 1").fetchone()
        self.assertEqual(row["progress"], 50.0)


class UpsertSectionsModulesFilesTests(RepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        repo.upsert_courses(
            self.conn,
            [Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )

    def test_full_hierarchy_upsert_is_idempotent(self) -> None:
        section = CourseSection(id=10, course_id=1, section_number=1, name="Semana 1",
                                 summary="", visible=True)
        module = CourseModule(id=100, course_id=1, section_id=10, modname="resource",
                               name="PDF", url=None, visible=True)
        file_ = CourseFile(module_id=100, filename="x.pdf", filepath="/", filesize=10,
                            mimetype="application/pdf", fileurl="https://x/x.pdf",
                            timemodified=123)

        for _ in range(2):
            repo.upsert_sections(self.conn, [section])
            repo.upsert_modules(self.conn, [module])
            repo.upsert_files(self.conn, [file_])

        self.assertEqual(len(self.conn.execute("SELECT * FROM course_sections").fetchall()), 1)
        self.assertEqual(len(self.conn.execute("SELECT * FROM course_modules").fetchall()), 1)
        self.assertEqual(len(self.conn.execute("SELECT * FROM course_files").fetchall()), 1)

    def test_get_course_modules_filters_by_course(self) -> None:
        repo.upsert_sections(
            self.conn, [CourseSection(id=10, course_id=1, section_number=1, name="S1",
                                       summary="", visible=True)]
        )
        repo.upsert_modules(
            self.conn,
            [CourseModule(id=100, course_id=1, section_id=10, modname="url", name="Link",
                           url="https://x", visible=True)],
        )

        modules = repo.get_course_modules(self.conn, course_id=1)
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0]["id"], 100)
        self.assertEqual(repo.get_course_modules(self.conn, course_id=999), [])


if __name__ == "__main__":
    unittest.main()

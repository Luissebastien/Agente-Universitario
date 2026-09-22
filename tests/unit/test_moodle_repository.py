import unittest

from database.db import connect
from database import moodle_repository as repo
from moodle.models import (
    Assignment,
    AssignmentSubmissionStatus,
    CalendarEvent,
    Course,
    CourseFile,
    CourseModule,
    CourseSection,
    Grade,
    MoodleUser,
)


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

    def test_get_course_returns_the_matching_row(self) -> None:
        repo.upsert_courses(self.conn, [
            Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
        ])
        row = repo.get_course(self.conn, 1)
        self.assertEqual(row["shortname"], "a")

    def test_get_course_returns_none_when_missing(self) -> None:
        self.assertIsNone(repo.get_course(self.conn, 999))

    def test_new_courses_have_no_classification_until_synced(self) -> None:
        repo.upsert_courses(self.conn, [
            Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
        ])
        row = repo.get_course(self.conn, 1)
        self.assertIsNone(row["timeline_classification"])


class SetCourseTimelineClassificationTests(RepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        repo.upsert_courses(self.conn, [
            Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
        ])

    def test_sets_the_classification(self) -> None:
        repo.set_course_timeline_classification(self.conn, 1, "inprogress")
        row = repo.get_course(self.conn, 1)
        self.assertEqual(row["timeline_classification"], "inprogress")

    def test_updating_again_overwrites_not_duplicates(self) -> None:
        repo.set_course_timeline_classification(self.conn, 1, "inprogress")
        repo.set_course_timeline_classification(self.conn, 1, "past")
        row = repo.get_course(self.conn, 1)
        self.assertEqual(row["timeline_classification"], "past")
        self.assertEqual(len(repo.get_courses(self.conn)), 1)

    def test_unknown_course_id_is_a_safe_no_op(self) -> None:
        repo.set_course_timeline_classification(self.conn, 999999, "inprogress")  # must not raise
        self.assertEqual(len(repo.get_courses(self.conn)), 1)  # unaffected


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


class UpsertAssignmentsTests(RepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        repo.upsert_courses(
            self.conn,
            [Course(id=4409, shortname="a", fullname="A", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )

    def test_upsert_twice_is_idempotent(self) -> None:
        assignment = Assignment(id=104926, course_id=4409, name="Tarea #1", duedate=1,
                                 allowsubmissionsfromdate=1, cutoffdate=0, grade=10)
        repo.upsert_assignments(self.conn, [assignment])
        repo.upsert_assignments(self.conn, [assignment])

        rows = repo.get_assignments(self.conn, course_id=4409)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Tarea #1")

    def test_update_changes_duedate(self) -> None:
        repo.upsert_assignments(
            self.conn,
            [Assignment(id=1, course_id=4409, name="x", duedate=100,
                        allowsubmissionsfromdate=None, cutoffdate=None, grade=None)],
        )
        repo.upsert_assignments(
            self.conn,
            [Assignment(id=1, course_id=4409, name="x", duedate=200,
                        allowsubmissionsfromdate=None, cutoffdate=None, grade=None)],
        )

        row = self.conn.execute("SELECT duedate FROM assignments WHERE id = 1").fetchone()
        self.assertEqual(row["duedate"], 200)

    def test_get_assignments_filters_by_course(self) -> None:
        repo.upsert_courses(
            self.conn,
            [Course(id=9999, shortname="b", fullname="B", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )
        repo.upsert_assignments(
            self.conn,
            [
                Assignment(id=1, course_id=4409, name="x", duedate=None,
                           allowsubmissionsfromdate=None, cutoffdate=None, grade=None),
                Assignment(id=2, course_id=9999, name="y", duedate=None,
                           allowsubmissionsfromdate=None, cutoffdate=None, grade=None),
            ],
        )
        self.assertEqual(len(repo.get_assignments(self.conn, course_id=4409)), 1)
        self.assertEqual(len(repo.get_assignments(self.conn, course_id=9999)), 1)


class UpsertAssignmentSubmissionStatusTests(RepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        repo.upsert_courses(
            self.conn,
            [Course(id=4409, shortname="a", fullname="A", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )
        repo.upsert_assignments(
            self.conn,
            [Assignment(id=1, course_id=4409, name="x", duedate=None,
                        allowsubmissionsfromdate=None, cutoffdate=None, grade=None)],
        )

    def test_upsert_twice_is_idempotent(self) -> None:
        status = AssignmentSubmissionStatus(
            assignment_id=1, submission_status="new", grading_status="notgraded",
            cansubmit=True, submitted_at=123,
        )
        repo.upsert_assignment_submission_status(self.conn, status)
        repo.upsert_assignment_submission_status(self.conn, status)

        rows = self.conn.execute("SELECT * FROM assignment_submission_status").fetchall()
        self.assertEqual(len(rows), 1)

    def test_update_reflects_grading_status_change(self) -> None:
        repo.upsert_assignment_submission_status(
            self.conn,
            AssignmentSubmissionStatus(assignment_id=1, submission_status="new",
                                        grading_status="notgraded", cansubmit=True,
                                        submitted_at=None),
        )
        repo.upsert_assignment_submission_status(
            self.conn,
            AssignmentSubmissionStatus(assignment_id=1, submission_status="submitted",
                                        grading_status="graded", cansubmit=False,
                                        submitted_at=456),
        )

        row = repo.get_assignment_submission_status(self.conn, assignment_id=1)
        self.assertEqual(row["grading_status"], "graded")
        self.assertEqual(row["submitted_at"], 456)

    def test_get_missing_status_returns_none(self) -> None:
        self.assertIsNone(repo.get_assignment_submission_status(self.conn, assignment_id=999))


class GetAssignmentsWithStatusTests(RepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        repo.upsert_courses(
            self.conn,
            [Course(id=4409, shortname="a", fullname="A", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )
        repo.upsert_assignments(
            self.conn,
            [
                Assignment(id=1, course_id=4409, name="with status", duedate=100,
                           allowsubmissionsfromdate=None, cutoffdate=None, grade=None),
                Assignment(id=2, course_id=4409, name="never synced", duedate=200,
                           allowsubmissionsfromdate=None, cutoffdate=None, grade=None),
            ],
        )
        repo.upsert_assignment_submission_status(
            self.conn,
            AssignmentSubmissionStatus(assignment_id=1, submission_status="new",
                                        grading_status="notgraded", cansubmit=True,
                                        submitted_at=None),
        )

    def test_left_join_keeps_assignments_with_no_synced_status(self) -> None:
        rows = repo.get_assignments_with_status(self.conn)
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[1]["submission_status"], "new")
        self.assertIsNone(by_id[2]["submission_status"])  # never synced, not dropped

    def test_filters_by_course_id(self) -> None:
        repo.upsert_courses(
            self.conn,
            [Course(id=9999, shortname="b", fullname="B", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )
        repo.upsert_assignments(
            self.conn,
            [Assignment(id=3, course_id=9999, name="other course", duedate=None,
                        allowsubmissionsfromdate=None, cutoffdate=None, grade=None)],
        )
        rows = repo.get_assignments_with_status(self.conn, course_id=4409)
        self.assertEqual({r["id"] for r in rows}, {1, 2})

    def test_get_single_assignment_with_status(self) -> None:
        row = repo.get_assignment_with_status(self.conn, 1)
        self.assertEqual(row["submission_status"], "new")

    def test_get_single_missing_assignment_returns_none(self) -> None:
        self.assertIsNone(repo.get_assignment_with_status(self.conn, 999))


class UpsertGradesTests(RepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        repo.upsert_courses(
            self.conn,
            [Course(id=4409, shortname="a", fullname="A", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )
        repo.upsert_user(self.conn, MoodleUser(id=15465, username="u", fullname="U"))

    def test_upsert_twice_is_idempotent(self) -> None:
        grade = Grade(id=185067, course_id=4409, user_id=15465, cmid=905633,
                       item_name="Tarea #1", item_type="mod", item_module="assign",
                       grade_raw=0.0, grade_formatted="0,00", percentage_formatted="0,00 %")
        repo.upsert_grades(self.conn, [grade])
        repo.upsert_grades(self.conn, [grade])

        rows = repo.get_grades(self.conn, course_id=4409)
        self.assertEqual(len(rows), 1)

    def test_grade_value_update_is_reflected(self) -> None:
        repo.upsert_grades(
            self.conn,
            [Grade(id=1, course_id=4409, user_id=15465, cmid=None, item_name="x",
                   item_type="mod", item_module="assign", grade_raw=None,
                   grade_formatted="-", percentage_formatted="-")],
        )
        repo.upsert_grades(
            self.conn,
            [Grade(id=1, course_id=4409, user_id=15465, cmid=None, item_name="x",
                   item_type="mod", item_module="assign", grade_raw=10.0,
                   grade_formatted="10,00", percentage_formatted="100,00 %")],
        )

        row = self.conn.execute("SELECT grade_raw FROM grades WHERE id = 1").fetchone()
        self.assertEqual(row["grade_raw"], 10.0)

    def test_get_all_grades_spans_every_course(self) -> None:
        repo.upsert_courses(
            self.conn,
            [Course(id=9999, shortname="b", fullname="B", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )
        repo.upsert_grades(
            self.conn,
            [
                Grade(id=1, course_id=4409, user_id=15465, cmid=None, item_name="x",
                      item_type="mod", item_module="assign", grade_raw=1.0,
                      grade_formatted="1", percentage_formatted="10 %"),
                Grade(id=2, course_id=9999, user_id=15465, cmid=None, item_name="y",
                      item_type="mod", item_module="assign", grade_raw=2.0,
                      grade_formatted="2", percentage_formatted="20 %"),
            ],
        )
        rows = repo.get_all_grades(self.conn)
        self.assertEqual({r["course_id"] for r in rows}, {4409, 9999})


class UpsertCalendarEventsTests(RepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        repo.upsert_courses(
            self.conn,
            [Course(id=4409, shortname="a", fullname="A", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )

    def test_upsert_twice_is_idempotent(self) -> None:
        event = CalendarEvent(id=807162, course_id=4409, name="Vencimiento", description=None,
                               eventtype="due", modulename="assign", instance=905633,
                               timestart=1, timesort=1, timeduration=0)
        repo.upsert_calendar_events(self.conn, [event])
        repo.upsert_calendar_events(self.conn, [event])

        rows = repo.get_calendar_events(self.conn, course_id=4409)
        self.assertEqual(len(rows), 1)

    def test_event_without_course_is_persisted(self) -> None:
        event = CalendarEvent(id=1, course_id=None, name="Global event", description=None,
                               eventtype="site", modulename=None, instance=None,
                               timestart=1, timesort=1, timeduration=0)
        repo.upsert_calendar_events(self.conn, [event])

        all_events = repo.get_calendar_events(self.conn)
        self.assertEqual(len(all_events), 1)
        self.assertIsNone(all_events[0]["course_id"])

    def test_get_calendar_events_filters_by_course(self) -> None:
        repo.upsert_calendar_events(
            self.conn,
            [
                CalendarEvent(id=1, course_id=4409, name="a", description=None, eventtype="due",
                              modulename=None, instance=None, timestart=1, timesort=1,
                              timeduration=0),
                CalendarEvent(id=2, course_id=None, name="b", description=None, eventtype="site",
                              modulename=None, instance=None, timestart=2, timesort=2,
                              timeduration=0),
            ],
        )
        self.assertEqual(len(repo.get_calendar_events(self.conn, course_id=4409)), 1)
        self.assertEqual(len(repo.get_calendar_events(self.conn)), 2)


if __name__ == "__main__":
    unittest.main()

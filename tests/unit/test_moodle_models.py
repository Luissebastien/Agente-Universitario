import unittest

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


class MoodleUserModelTests(unittest.TestCase):
    def test_from_site_info(self) -> None:
        user = MoodleUser.from_site_info(
            {"userid": 15465, "username": "1130421", "fullname": "LUIS JIMENEZ"}
        )
        self.assertEqual(user, MoodleUser(id=15465, username="1130421", fullname="LUIS JIMENEZ"))


class CourseModelTests(unittest.TestCase):
    def test_from_moodle_maps_known_fields(self) -> None:
        course = Course.from_moodle(
            {
                "id": 22055,
                "shortname": "ING-IDS343-02_2026-3",
                "fullname": "ESTRUCTURAS DE DATOS Y ALGORITMOS I",
                "category": 310,
                "visible": 1,
                "progress": 40.5,
                "startdate": 1785729600,
                "enddate": 1794023940,
            }
        )
        self.assertEqual(course.id, 22055)
        self.assertEqual(course.category, 310)
        self.assertTrue(course.visible)
        self.assertAlmostEqual(course.progress, 40.5)

    def test_missing_optional_fields_default_sensibly(self) -> None:
        course = Course.from_moodle({"id": 1})
        self.assertEqual(course.shortname, "")
        self.assertEqual(course.fullname, "")
        self.assertIsNone(course.category)
        self.assertTrue(course.visible)  # Moodle default: visible unless said otherwise
        self.assertIsNone(course.progress)


class CourseSectionModelTests(unittest.TestCase):
    def test_from_moodle(self) -> None:
        section = CourseSection.from_moodle(
            22055, {"id": 501, "section": 1, "name": "Semana 1", "summary": "", "visible": 1}
        )
        self.assertEqual(section.id, 501)
        self.assertEqual(section.course_id, 22055)
        self.assertEqual(section.section_number, 1)


class CourseModuleModelTests(unittest.TestCase):
    def test_from_moodle(self) -> None:
        module = CourseModule.from_moodle(
            22055,
            501,
            {"id": 905633, "modname": "assign", "name": "Tarea #1", "url": "https://x", "visible": 1},
        )
        self.assertEqual(module.id, 905633)
        self.assertEqual(module.course_id, 22055)
        self.assertEqual(module.section_id, 501)
        self.assertEqual(module.modname, "assign")


class CourseFileModelTests(unittest.TestCase):
    def test_from_moodle(self) -> None:
        file_ = CourseFile.from_moodle(
            37443,
            {
                "filename": "programamat5.pdf",
                "filepath": "/",
                "filesize": 27223,
                "mimetype": "application/pdf",
                "fileurl": "https://campusvirtual.intec.edu.do/webservice/pluginfile.php/242296/mod_resource/content/0/programamat5.pdf",
                "timemodified": 1673031876,
            },
        )
        self.assertEqual(file_.module_id, 37443)
        self.assertEqual(file_.filesize, 27223)
        self.assertEqual(file_.mimetype, "application/pdf")


class AssignmentModelTests(unittest.TestCase):
    def test_from_moodle(self) -> None:
        assignment = Assignment.from_moodle(
            4409,
            {
                "id": 104926,
                "name": "Tarea #1 de ED",
                "duedate": 1786852500,
                "allowsubmissionsfromdate": 1786407000,
                "cutoffdate": 0,
                "grade": 10,
            },
        )
        self.assertEqual(assignment.id, 104926)
        self.assertEqual(assignment.course_id, 4409)
        self.assertEqual(assignment.grade, 10)


class AssignmentSubmissionStatusModelTests(unittest.TestCase):
    def test_from_moodle_with_submission(self) -> None:
        status = AssignmentSubmissionStatus.from_moodle(
            104926,
            {
                "lastattempt": {
                    "gradingstatus": "notgraded",
                    "cansubmit": False,
                    "submission": {"status": "new", "timemodified": 1786795183},
                },
            },
        )
        self.assertEqual(status.assignment_id, 104926)
        self.assertEqual(status.submission_status, "new")
        self.assertEqual(status.grading_status, "notgraded")
        self.assertFalse(status.cansubmit)
        self.assertEqual(status.submitted_at, 1786795183)

    def test_from_moodle_without_submission_object(self) -> None:
        # Real Moodle response for an assignment with no attempt yet: no
        # 'submission' key at all inside lastattempt.
        status = AssignmentSubmissionStatus.from_moodle(
            91719,
            {"lastattempt": {"gradingstatus": "notgraded", "cansubmit": False}},
        )
        self.assertIsNone(status.submission_status)
        self.assertIsNone(status.submitted_at)
        self.assertEqual(status.grading_status, "notgraded")

    def test_from_moodle_with_missing_lastattempt(self) -> None:
        status = AssignmentSubmissionStatus.from_moodle(1, {})
        self.assertIsNone(status.grading_status)
        self.assertIsNone(status.submission_status)


class GradeModelTests(unittest.TestCase):
    def test_from_moodle(self) -> None:
        grade = Grade.from_moodle(
            4409,
            15465,
            {
                "id": 185067,
                "cmid": 905633,
                "itemname": "Tarea #1 de ED",
                "itemtype": "mod",
                "itemmodule": "assign",
                "graderaw": 0,
                "gradeformatted": "0,00",
                "percentageformatted": "0,00 %",
            },
        )
        self.assertEqual(grade.id, 185067)
        self.assertEqual(grade.course_id, 4409)
        self.assertEqual(grade.user_id, 15465)
        self.assertEqual(grade.cmid, 905633)
        self.assertEqual(grade.item_module, "assign")
        self.assertEqual(grade.grade_raw, 0)

    def test_course_level_item_has_no_cmid(self) -> None:
        grade = Grade.from_moodle(
            4409, 15465, {"id": 43466, "itemtype": "course", "graderaw": None}
        )
        self.assertIsNone(grade.cmid)
        self.assertIsNone(grade.item_module)


class CalendarEventModelTests(unittest.TestCase):
    def test_from_moodle_extracts_nested_course_id(self) -> None:
        event = CalendarEvent.from_moodle(
            {
                "id": 807162,
                "name": "Vencimiento de Tarea #1 de ED",
                "description": "<p>valor de 10 puntos.</p>",
                "eventtype": "due",
                "modulename": "assign",
                "instance": 905633,
                "timestart": 1786852500,
                "timesort": 1786852500,
                "timeduration": 0,
                "course": {"id": 4409, "fullname": "ECUACIONES DIFERENCIALES"},
            }
        )
        self.assertEqual(event.id, 807162)
        self.assertEqual(event.course_id, 4409)
        self.assertEqual(event.eventtype, "due")
        self.assertEqual(event.description, "valor de 10 puntos.")
        self.assertEqual(event.timesort, 1786852500)
        self.assertEqual(event.timeduration, 0)

    def test_from_moodle_without_course(self) -> None:
        event = CalendarEvent.from_moodle(
            {"id": 1, "name": "x", "eventtype": "due", "timestart": 0}
        )
        self.assertIsNone(event.course_id)
        self.assertIsNone(event.description)

    def test_from_moodle_without_description(self) -> None:
        event = CalendarEvent.from_moodle(
            {"id": 1, "name": "x", "eventtype": "due", "timestart": 0, "description": ""}
        )
        self.assertIsNone(event.description)


if __name__ == "__main__":
    unittest.main()

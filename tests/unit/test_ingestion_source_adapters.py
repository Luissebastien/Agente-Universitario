import unittest

from database.db import connect
from database import moodle_repository as moodle_repo
from ingestion.source_adapters import MoodleFileSourceAdapter, MoodleUrlSourceAdapter
from moodle.models import Course, CourseFile, CourseModule, CourseSection


class SourceAdapterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        moodle_repo.upsert_courses(
            self.conn,
            [Course(id=4409, shortname="a", fullname="A", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )
        moodle_repo.upsert_sections(
            self.conn,
            [CourseSection(id=501, course_id=4409, section_number=1, name="Semana 1",
                            summary="", visible=True)],
        )

    def tearDown(self) -> None:
        self.conn.close()


class MoodleFileSourceAdapterTests(SourceAdapterTestCase):
    def test_translates_course_file_into_descriptor(self) -> None:
        moodle_repo.upsert_modules(
            self.conn,
            [CourseModule(id=905633, course_id=4409, section_id=501, modname="resource",
                           name="Programa", url=None, visible=True)],
        )
        moodle_repo.upsert_files(
            self.conn,
            [CourseFile(module_id=905633, filename="programa.pdf", filepath="/", filesize=100,
                        mimetype="application/pdf",
                        fileurl="https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/programa.pdf",
                        timemodified=123)],
        )

        descriptors = list(MoodleFileSourceAdapter().discover(self.conn))

        self.assertEqual(len(descriptors), 1)
        d = descriptors[0]
        self.assertEqual(d.origin, "moodle")
        self.assertEqual(d.source_type, "file")
        self.assertEqual(d.external_reference, d.source_url)
        self.assertEqual(d.course_id, 4409)
        self.assertEqual(d.section_id, 501)
        self.assertEqual(d.module_id, 905633)
        self.assertEqual(d.name, "programa.pdf")
        self.assertEqual(d.mimetype, "application/pdf")

    def test_no_files_yields_nothing(self) -> None:
        self.assertEqual(list(MoodleFileSourceAdapter().discover(self.conn)), [])


class MoodleUrlSourceAdapterTests(SourceAdapterTestCase):
    def test_detects_url_modules_only(self) -> None:
        moodle_repo.upsert_modules(
            self.conn,
            [
                CourseModule(id=1, course_id=4409, section_id=501, modname="url",
                             name="Calendario Institucional", url="https://www.intec.edu.do/calendarios",
                             visible=True),
                CourseModule(id=2, course_id=4409, section_id=501, modname="resource",
                             name="No es un link", url=None, visible=True),
                CourseModule(id=3, course_id=4409, section_id=501, modname="label",
                             name="Texto", url=None, visible=True),
            ],
        )

        descriptors = list(MoodleUrlSourceAdapter().discover(self.conn))

        self.assertEqual(len(descriptors), 1)
        d = descriptors[0]
        self.assertEqual(d.source_type, "url")
        self.assertEqual(d.source_url, "https://www.intec.edu.do/calendarios")
        self.assertEqual(d.external_reference, "1")
        self.assertEqual(d.module_id, 1)
        # No content interpretation: mimetype is not guessed for a URL reference.
        self.assertIsNone(d.mimetype)

    def test_url_module_without_url_value_is_ignored(self) -> None:
        moodle_repo.upsert_modules(
            self.conn,
            [CourseModule(id=1, course_id=4409, section_id=501, modname="url",
                           name="Broken", url=None, visible=True)],
        )

        self.assertEqual(list(MoodleUrlSourceAdapter().discover(self.conn)), [])

    def test_no_url_modules_yields_nothing(self) -> None:
        self.assertEqual(list(MoodleUrlSourceAdapter().discover(self.conn)), [])


if __name__ == "__main__":
    unittest.main()

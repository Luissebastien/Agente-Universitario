import time
import unittest

from database import extraction_repository as extraction_repo
from database import ingestion_repository as ingestion_repo
from database import moodle_repository as moodle_repo
from database.db import connect
from extraction.models import ExtractedDocument
from ingestion.models import ResourceDescriptor, ResourceVersion
from moodle.models import (
    Assignment,
    AssignmentSubmissionStatus,
    CalendarEvent,
    Course,
    Grade,
    MoodleUser,
)
from read_model import AcademicReadModel

NOW = int(time.time())
DAY = 86_400


class ReadModelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.read_model = AcademicReadModel(self.conn)

    def tearDown(self) -> None:
        self.conn.close()


class CourseTests(ReadModelTestCase):
    def setUp(self) -> None:
        super().setUp()
        moodle_repo.upsert_courses(self.conn, [
            Course(id=1, shortname="current", fullname="Current Course", category=10,
                   visible=True, progress=50.0, startdate=NOW - DAY, enddate=NOW + DAY),
            Course(id=2, shortname="past", fullname="Past Course", category=10,
                   visible=True, progress=100.0, startdate=NOW - 400 * DAY, enddate=NOW - 300 * DAY),
            Course(id=3, shortname="unclassified", fullname="Never Classified", category=10,
                   visible=True, progress=None, startdate=None, enddate=None),
        ])
        moodle_repo.set_course_timeline_classification(self.conn, 1, "inprogress")
        moodle_repo.set_course_timeline_classification(self.conn, 2, "past")
        # course 3 deliberately never gets a classification synced

    def test_get_current_courses_returns_only_inprogress(self) -> None:
        current = self.read_model.get_current_courses()
        self.assertEqual([c.id for c in current], [1])
        self.assertTrue(current[0].is_current)

    def test_unclassified_course_is_not_current(self) -> None:
        # Unknown classification must never be treated as "current" (fail closed).
        current_ids = {c.id for c in self.read_model.get_current_courses()}
        self.assertNotIn(3, current_ids)

    def test_get_courses_preserves_historical_courses(self) -> None:
        all_courses = self.read_model.get_courses()
        self.assertEqual({c.id for c in all_courses}, {1, 2, 3})
        past = next(c for c in all_courses if c.id == 2)
        self.assertFalse(past.is_current)
        self.assertEqual(past.timeline_classification, "past")

    def test_get_course_by_id(self) -> None:
        course = self.read_model.get_course(1)
        self.assertEqual(course.fullname, "Current Course")

    def test_get_nonexistent_course_returns_none(self) -> None:
        self.assertIsNone(self.read_model.get_course(999999))


class AssignmentTests(ReadModelTestCase):
    def setUp(self) -> None:
        super().setUp()
        moodle_repo.upsert_courses(self.conn, [
            Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
            Course(id=2, shortname="b", fullname="B", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
        ])
        moodle_repo.upsert_assignments(self.conn, [
            Assignment(id=1, course_id=1, name="future not submitted", duedate=NOW + DAY,
                       allowsubmissionsfromdate=None, cutoffdate=None, grade=100),
            Assignment(id=2, course_id=1, name="future submitted", duedate=NOW + DAY,
                       allowsubmissionsfromdate=None, cutoffdate=None, grade=100),
            Assignment(id=3, course_id=1, name="past not submitted", duedate=NOW - DAY,
                       allowsubmissionsfromdate=None, cutoffdate=None, grade=100),
            Assignment(id=4, course_id=1, name="no due date", duedate=0,
                       allowsubmissionsfromdate=None, cutoffdate=None, grade=100),
            Assignment(id=5, course_id=1, name="status never synced", duedate=NOW + DAY,
                       allowsubmissionsfromdate=None, cutoffdate=None, grade=100),
            Assignment(id=6, course_id=2, name="other course", duedate=NOW + DAY,
                       allowsubmissionsfromdate=None, cutoffdate=None, grade=100),
        ])
        for aid, status in [(1, "new"), (2, "submitted"), (3, "new"), (4, "new"), (6, "new")]:
            moodle_repo.upsert_assignment_submission_status(
                self.conn,
                AssignmentSubmissionStatus(assignment_id=aid, submission_status=status,
                                            grading_status="notgraded", cansubmit=True,
                                            submitted_at=None),
            )
        # assignment 5's status deliberately never synced

    def test_get_assignments_filters_by_course(self) -> None:
        course1 = self.read_model.get_assignments(course_id=1)
        self.assertEqual({a.id for a in course1}, {1, 2, 3, 4, 5})

    def test_get_assignments_with_no_filter_returns_all_courses(self) -> None:
        all_assignments = self.read_model.get_assignments()
        self.assertEqual({a.id for a in all_assignments}, {1, 2, 3, 4, 5, 6})

    def test_get_single_assignment(self) -> None:
        assignment = self.read_model.get_assignment(1)
        self.assertEqual(assignment.name, "future not submitted")

    def test_get_nonexistent_assignment_returns_none(self) -> None:
        self.assertIsNone(self.read_model.get_assignment(999999))

    def test_pending_assignments_future_not_submitted_included(self) -> None:
        pending_ids = {a.id for a in self.read_model.get_pending_assignments()}
        self.assertIn(1, pending_ids)

    def test_pending_assignments_future_submitted_excluded(self) -> None:
        pending_ids = {a.id for a in self.read_model.get_pending_assignments()}
        self.assertNotIn(2, pending_ids)

    def test_pending_assignments_past_not_submitted_excluded(self) -> None:
        # "pending" is defined as próxima - a late unsubmitted assignment
        # does not qualify under this definition.
        pending_ids = {a.id for a in self.read_model.get_pending_assignments()}
        self.assertNotIn(3, pending_ids)

    def test_pending_assignments_no_due_date_excluded(self) -> None:
        pending_ids = {a.id for a in self.read_model.get_pending_assignments()}
        self.assertNotIn(4, pending_ids)

    def test_pending_assignments_unknown_status_excluded(self) -> None:
        pending_ids = {a.id for a in self.read_model.get_pending_assignments()}
        self.assertNotIn(5, pending_ids)

    def test_pending_assignments_exact_expected_set(self) -> None:
        pending_ids = {a.id for a in self.read_model.get_pending_assignments()}
        self.assertEqual(pending_ids, {1, 6})

    def test_upcoming_assignments_default_window(self) -> None:
        upcoming_ids = {a.id for a in self.read_model.get_upcoming_assignments()}
        self.assertEqual(upcoming_ids, {1, 2, 5, 6})  # due tomorrow, regardless of status

    def test_upcoming_assignments_excludes_past_and_undated(self) -> None:
        upcoming_ids = {a.id for a in self.read_model.get_upcoming_assignments()}
        self.assertNotIn(3, upcoming_ids)  # past due
        self.assertNotIn(4, upcoming_ids)  # no due date


class CalendarTests(ReadModelTestCase):
    def setUp(self) -> None:
        super().setUp()
        moodle_repo.upsert_courses(self.conn, [
            Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
        ])
        moodle_repo.upsert_calendar_events(self.conn, [
            CalendarEvent(id=1, course_id=1, name="inside range", description=None,
                           eventtype="due", modulename="assign", instance=1,
                           timestart=NOW, timesort=NOW, timeduration=0),
            CalendarEvent(id=2, course_id=1, name="before range", description=None,
                           eventtype="due", modulename="assign", instance=2,
                           timestart=NOW - 10 * DAY, timesort=NOW - 10 * DAY, timeduration=0),
            CalendarEvent(id=3, course_id=1, name="after range", description=None,
                           eventtype="due", modulename="assign", instance=3,
                           timestart=NOW + 10 * DAY, timesort=NOW + 10 * DAY, timeduration=0),
            CalendarEvent(id=4, course_id=None, name="no course", description=None,
                           eventtype="site", modulename=None, instance=None,
                           timestart=NOW, timesort=NOW, timeduration=0),
        ])

    def test_range_includes_only_events_within_bounds(self) -> None:
        events = self.read_model.get_calendar_events(NOW - DAY, NOW + DAY)
        self.assertEqual({e.id for e in events}, {1, 4})

    def test_range_boundaries_are_inclusive(self) -> None:
        events = self.read_model.get_calendar_events(NOW, NOW)
        self.assertEqual({e.id for e in events}, {1, 4})

    def test_course_scoped_events(self) -> None:
        events = self.read_model.get_course_calendar_events(1, NOW - DAY, NOW + DAY)
        self.assertEqual({e.id for e in events}, {1})  # course-scoped excludes the no-course event


class GradeTests(ReadModelTestCase):
    def setUp(self) -> None:
        super().setUp()
        moodle_repo.upsert_courses(self.conn, [
            Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
            Course(id=2, shortname="b", fullname="B", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
        ])
        moodle_repo.upsert_user(self.conn, MoodleUser(id=1, username="u", fullname="U"))
        moodle_repo.upsert_grades(self.conn, [
            Grade(id=1, course_id=1, user_id=1, cmid=None, item_name="Tarea 1", item_type="mod",
                  item_module="assign", grade_raw=85.0, grade_formatted="85,00",
                  percentage_formatted="85,00 %"),
            Grade(id=2, course_id=2, user_id=1, cmid=None, item_name="Tarea 2", item_type="mod",
                  item_module="assign", grade_raw=70.0, grade_formatted="70,00",
                  percentage_formatted="70,00 %"),
        ])

    def test_returns_moodle_values_unmodified(self) -> None:
        grades = self.read_model.get_grades(course_id=1)
        self.assertEqual(grades[0].grade_raw, 85.0)
        self.assertEqual(grades[0].grade_formatted, "85,00")
        self.assertEqual(grades[0].percentage_formatted, "85,00 %")

    def test_filters_by_course(self) -> None:
        grades = self.read_model.get_grades(course_id=1)
        self.assertEqual([g.id for g in grades], [1])

    def test_no_course_filter_spans_all_courses(self) -> None:
        grades = self.read_model.get_grades()
        self.assertEqual({g.id for g in grades}, {1, 2})

    def test_no_computed_fields_are_invented(self) -> None:
        # Grade the dataclass simply has no average/GPA/prediction fields -
        # this test documents that constraint rather than asserting a value.
        grade = self.read_model.get_grades(course_id=1)[0]
        self.assertFalse(hasattr(grade, "average"))
        self.assertFalse(hasattr(grade, "gpa"))


class MaterialsTests(ReadModelTestCase):
    def setUp(self) -> None:
        super().setUp()
        moodle_repo.upsert_courses(self.conn, [
            Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
        ])

    def _make_resource(self, external_ref: str, course_id: int = 1) -> int:
        descriptor = ResourceDescriptor(
            origin="moodle", source_type="file", external_reference=external_ref,
            course_id=course_id, section_id=None, module_id=None, name=f"{external_ref}.pdf",
            source_url=f"https://x/{external_ref}", mimetype="application/pdf",
        )
        return ingestion_repo.upsert_resource(self.conn, descriptor).id

    def test_resource_with_no_version_appears_with_none_fields(self) -> None:
        self._make_resource("no-version")
        materials = self.read_model.get_course_materials(1)
        self.assertEqual(len(materials), 1)
        self.assertIsNone(materials[0].latest_version_id)
        self.assertIsNone(materials[0].extraction_status)

    def test_resource_with_version_but_no_extraction(self) -> None:
        resource_id = self._make_resource("version-only")
        ingestion_repo.insert_resource_version(
            self.conn,
            ResourceVersion(id=None, resource_id=resource_id, version_number=1,
                             content_hash="h1", storage_ref="h1", size_bytes=100,
                             mimetype="application/pdf", ingested_at="2026-01-01T00:00:00+00:00"),
        )
        materials = self.read_model.get_course_materials(1)
        self.assertEqual(materials[0].latest_version_number, 1)
        self.assertIsNone(materials[0].extraction_status)

    def test_resource_with_version_and_extraction(self) -> None:
        resource_id = self._make_resource("fully-extracted")
        version = ingestion_repo.insert_resource_version(
            self.conn,
            ResourceVersion(id=None, resource_id=resource_id, version_number=1,
                             content_hash="h2", storage_ref="h2", size_bytes=200,
                             mimetype="application/pdf", ingested_at="2026-01-01T00:00:00+00:00"),
        )
        extraction_repo.insert_extracted_document(
            self.conn,
            ExtractedDocument(id=None, resource_version_id=version.id, depth="basic",
                               extractor_name="pdf_hybrid", extractor_version="1", status="done",
                               error_reason=None, extracted_text="some text", metadata={},
                               extracted_at="2026-01-01T00:00:00+00:00"),
        )
        materials = self.read_model.get_course_materials(1)
        self.assertEqual(materials[0].extraction_status, "done")
        self.assertEqual(materials[0].extractor_name, "pdf_hybrid")

    def test_no_duplication_multiple_versions_and_attempts_yield_one_row(self) -> None:
        resource_id = self._make_resource("multi-version")
        v1 = ingestion_repo.insert_resource_version(
            self.conn,
            ResourceVersion(id=None, resource_id=resource_id, version_number=1,
                             content_hash="a", storage_ref="a", size_bytes=1,
                             mimetype="application/pdf", ingested_at="2026-01-01T00:00:00+00:00"),
        )
        v2 = ingestion_repo.insert_resource_version(
            self.conn,
            ResourceVersion(id=None, resource_id=resource_id, version_number=2,
                             content_hash="b", storage_ref="b", size_bytes=2,
                             mimetype="application/pdf", ingested_at="2026-01-02T00:00:00+00:00"),
        )
        for _ in range(3):  # three extraction attempts against the latest version (append-only)
            extraction_repo.insert_extracted_document(
                self.conn,
                ExtractedDocument(id=None, resource_version_id=v2.id, depth="basic",
                                   extractor_name="pdf_hybrid", extractor_version="1",
                                   status="done", error_reason=None, extracted_text="x",
                                   metadata={}, extracted_at="2026-01-02T00:00:00+00:00"),
            )
        materials = self.read_model.get_course_materials(1)
        self.assertEqual(len(materials), 1)  # exactly one row for this resource
        self.assertEqual(materials[0].latest_version_number, 2)  # the newer version, not v1

    def test_extracted_text_is_never_loaded_by_materials_query(self) -> None:
        resource_id = self._make_resource("no-text-leak")
        version = ingestion_repo.insert_resource_version(
            self.conn,
            ResourceVersion(id=None, resource_id=resource_id, version_number=1,
                             content_hash="c", storage_ref="c", size_bytes=1,
                             mimetype="application/pdf", ingested_at="2026-01-01T00:00:00+00:00"),
        )
        extraction_repo.insert_extracted_document(
            self.conn,
            ExtractedDocument(id=None, resource_version_id=version.id, depth="basic",
                               extractor_name="pdf_hybrid", extractor_version="1", status="done",
                               error_reason=None, extracted_text="A" * 10_000, metadata={},
                               extracted_at="2026-01-01T00:00:00+00:00"),
        )
        material = self.read_model.get_course_materials(1)[0]
        self.assertFalse(hasattr(material, "extracted_text"))

    def test_get_resource_returns_provenance(self) -> None:
        resource_id = self._make_resource("provenance-check")
        resource = self.read_model.get_resource(resource_id)
        self.assertEqual(resource.origin, "moodle")
        self.assertEqual(resource.course_id, 1)

    def test_get_nonexistent_resource_returns_none(self) -> None:
        self.assertIsNone(self.read_model.get_resource(999999))


class ExtractedDocumentTests(ReadModelTestCase):
    def setUp(self) -> None:
        super().setUp()
        moodle_repo.upsert_courses(self.conn, [
            Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
        ])
        descriptor = ResourceDescriptor(
            origin="moodle", source_type="file", external_reference="x",
            course_id=1, section_id=None, module_id=None, name="x.pdf",
            source_url="https://x/x.pdf", mimetype="application/pdf",
        )
        resource = ingestion_repo.upsert_resource(self.conn, descriptor)
        self.version = ingestion_repo.insert_resource_version(
            self.conn,
            ResourceVersion(id=None, resource_id=resource.id, version_number=1,
                             content_hash="h", storage_ref="h", size_bytes=1,
                             mimetype="application/pdf", ingested_at="2026-01-01T00:00:00+00:00"),
        )

    def test_preserves_full_provenance(self) -> None:
        extraction_repo.insert_extracted_document(
            self.conn,
            ExtractedDocument(id=None, resource_version_id=self.version.id, depth="basic",
                               extractor_name="pdf_hybrid", extractor_version="1", status="done",
                               error_reason=None, extracted_text="real text",
                               metadata={"pages": 3}, extracted_at="2026-01-01T00:00:00+00:00"),
        )
        doc = self.read_model.get_extracted_document(self.version.id)
        self.assertEqual(doc.resource_version_id, self.version.id)
        self.assertEqual(doc.extractor_name, "pdf_hybrid")
        self.assertEqual(doc.extractor_version, "1")
        self.assertEqual(doc.depth, "basic")
        self.assertEqual(doc.status, "done")
        self.assertEqual(doc.metadata, {"pages": 3})
        self.assertEqual(doc.extracted_text, "real text")

    def test_preserves_failure_reason(self) -> None:
        extraction_repo.insert_extracted_document(
            self.conn,
            ExtractedDocument(id=None, resource_version_id=self.version.id, depth="basic",
                               extractor_name="none", extractor_version="n/a", status="failed",
                               error_reason="unsupported format", extracted_text=None,
                               metadata={}, extracted_at="2026-01-01T00:00:00+00:00"),
        )
        doc = self.read_model.get_extracted_document(self.version.id)
        self.assertEqual(doc.status, "failed")
        self.assertEqual(doc.error_reason, "unsupported format")

    def test_missing_extraction_returns_none(self) -> None:
        self.assertIsNone(self.read_model.get_extracted_document(self.version.id))


class EmptyStateTests(ReadModelTestCase):
    def test_no_courses(self) -> None:
        self.assertEqual(self.read_model.get_courses(), [])
        self.assertEqual(self.read_model.get_current_courses(), [])

    def test_no_assignments(self) -> None:
        self.assertEqual(self.read_model.get_assignments(), [])
        self.assertEqual(self.read_model.get_pending_assignments(), [])
        self.assertEqual(self.read_model.get_upcoming_assignments(), [])

    def test_no_calendar_events(self) -> None:
        self.assertEqual(self.read_model.get_calendar_events(0, NOW + 1000), [])

    def test_no_grades(self) -> None:
        self.assertEqual(self.read_model.get_grades(), [])

    def test_no_materials(self) -> None:
        moodle_repo.upsert_courses(self.conn, [
            Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
        ])
        self.assertEqual(self.read_model.get_course_materials(1), [])


class ReadOnlyTests(ReadModelTestCase):
    """Confirms Read Model methods never write to the database."""

    def setUp(self) -> None:
        super().setUp()
        moodle_repo.upsert_courses(self.conn, [
            Course(id=1, shortname="a", fullname="A", category=None, visible=True,
                   progress=None, startdate=None, enddate=None),
        ])
        moodle_repo.set_course_timeline_classification(self.conn, 1, "inprogress")
        moodle_repo.upsert_assignments(self.conn, [
            Assignment(id=1, course_id=1, name="x", duedate=NOW + DAY,
                       allowsubmissionsfromdate=None, cutoffdate=None, grade=None),
        ])

    def _table_counts(self) -> dict[str, int]:
        tables = ["courses", "assignments", "assignment_submission_status", "grades",
                  "calendar_events", "resources", "resource_versions", "extracted_documents"]
        return {
            t: self.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables
        }

    def test_read_calls_do_not_change_row_counts(self) -> None:
        before = self._table_counts()

        self.read_model.get_courses()
        self.read_model.get_current_courses()
        self.read_model.get_course(1)
        self.read_model.get_assignments()
        self.read_model.get_pending_assignments()
        self.read_model.get_upcoming_assignments()
        self.read_model.get_calendar_events(0, NOW + 1000)
        self.read_model.get_course_calendar_events(1, 0, NOW + 1000)
        self.read_model.get_grades()
        self.read_model.get_course_materials(1)
        self.read_model.get_resource(999999)
        self.read_model.get_extracted_document(999999)

        after = self._table_counts()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

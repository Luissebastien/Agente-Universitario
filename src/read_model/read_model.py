from __future__ import annotations

import sqlite3
import time

from database import extraction_repository as extraction_repo
from database import ingestion_repository as ingestion_repo
from database import moodle_repository as moodle_repo
from extraction.models import ExtractedDocument
from ingestion.models import Resource
from moodle.models import CalendarEvent, Grade
from read_model.models import AssignmentSummary, CourseSummary, MaterialSummary

_DAY_SECONDS = 86_400
_DEFAULT_DEPTH = "basic"


def is_assignment_pending(due_date: int | None, submission_status: str | None, now: int) -> bool:
    """The accepted, tested definition of "pending" (confirmed 2026-09-17):

    1. the assignment has a real due date (Moodle: duedate == 0 means "no
       due date set" - treated the same as None, never "próxima");
    2. that due date has not passed (>= now);
    3. the student's submission status is KNOWN (synced) and is not
       "submitted".

    An unknown status (never synced - see AssignmentSummary's docstring) is
    NOT treated as "not submitted" - that would be inventing a fact Moodle
    hasn't confirmed (DEC-013, fail closed). It is excluded, not assumed.
    """
    if not due_date:
        return False
    if due_date < now:
        return False
    if submission_status is None:
        return False
    return submission_status != "submitted"


def is_assignment_upcoming(due_date: int | None, now: int, days: int) -> bool:
    """due date falls within [now, now + days] - regardless of submission
    status (an already-submitted assignment can still be "upcoming"; that's
    a different question from "pending", see is_assignment_pending)."""
    if not due_date:
        return False
    return now <= due_date <= now + days * _DAY_SECONDS


def _row_to_course_summary(row: sqlite3.Row) -> CourseSummary:
    classification = row["timeline_classification"]
    return CourseSummary(
        id=row["id"], shortname=row["shortname"], fullname=row["fullname"],
        category=row["category"], is_current=(classification == "inprogress"),
        timeline_classification=classification, progress=row["progress"],
        startdate=row["startdate"], enddate=row["enddate"], last_synced_at=row["last_synced_at"],
    )


def _row_to_assignment_summary(row: sqlite3.Row, now: int) -> AssignmentSummary:
    return AssignmentSummary(
        id=row["id"], course_id=row["course_id"], name=row["name"], duedate=row["duedate"],
        allowsubmissionsfromdate=row["allowsubmissionsfromdate"], cutoffdate=row["cutoffdate"],
        grade=row["grade"], submission_status=row["submission_status"],
        grading_status=row["grading_status"],
        is_pending=is_assignment_pending(row["duedate"], row["submission_status"], now),
    )


def _row_to_calendar_event(row: sqlite3.Row) -> CalendarEvent:
    return CalendarEvent(
        id=row["id"], course_id=row["course_id"], name=row["name"],
        description=row["description"], eventtype=row["eventtype"],
        modulename=row["modulename"], instance=row["instance"], timestart=row["timestart"],
        timesort=row["timesort"], timeduration=row["timeduration"],
    )


def _row_to_grade(row: sqlite3.Row) -> Grade:
    return Grade(
        id=row["id"], course_id=row["course_id"], user_id=row["user_id"], cmid=row["cmid"],
        item_name=row["item_name"], item_type=row["item_type"], item_module=row["item_module"],
        grade_raw=row["grade_raw"], grade_formatted=row["grade_formatted"],
        percentage_formatted=row["percentage_formatted"],
    )


_MATERIALS_SQL = """
    SELECT
        r.id AS resource_id, r.name, r.course_id, r.section_id, r.module_id,
        r.source_type, r.source_url,
        rv.id AS latest_version_id, rv.version_number AS latest_version_number,
        rv.mimetype, rv.size_bytes, rv.ingested_at,
        ed.status AS extraction_status, ed.extractor_name, ed.extracted_at
    FROM resources r
    LEFT JOIN resource_versions rv
        ON rv.resource_id = r.id
        AND rv.version_number = (
            SELECT MAX(v2.version_number) FROM resource_versions v2 WHERE v2.resource_id = r.id
        )
    LEFT JOIN extracted_documents ed
        ON ed.resource_version_id = rv.id
        AND ed.id = (
            SELECT MAX(e2.id) FROM extracted_documents e2 WHERE e2.resource_version_id = rv.id
        )
    WHERE r.course_id = ?
    ORDER BY r.name
"""


def _row_to_material_summary(row: sqlite3.Row) -> MaterialSummary:
    return MaterialSummary(
        resource_id=row["resource_id"], name=row["name"], course_id=row["course_id"],
        section_id=row["section_id"], module_id=row["module_id"],
        source_type=row["source_type"], source_url=row["source_url"],
        latest_version_id=row["latest_version_id"],
        latest_version_number=row["latest_version_number"], mimetype=row["mimetype"],
        size_bytes=row["size_bytes"], ingested_at=row["ingested_at"],
        extraction_status=row["extraction_status"], extractor_name=row["extractor_name"],
        extracted_at=row["extracted_at"],
    )


class AcademicReadModel:
    """Read-only view over the locally persisted academic data.

    Reads exclusively from the SQLite connection it is given - never calls
    Moodle, never downloads a file, never runs Extraction, never touches an
    LLM. Every method is a plain SELECT (or a small number of them); nothing
    here writes to the database. See DEC-060 onwards in .ai/decisions.md.

    Returns read models (or, where an existing dataclass already fits,
    that existing type - see read_model/models.py) rather than raw
    sqlite3.Row objects, so a future consumer (RAG, Agent) never needs to
    know the SQLite schema.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ---- Courses -----------------------------------------------------

    def get_courses(self) -> list[CourseSummary]:
        """Every synced course, current and historical alike - nothing is
        ever excluded here (see get_current_courses() for the filtered view)."""
        return [_row_to_course_summary(r) for r in moodle_repo.get_courses(self._conn)]

    def get_current_courses(self) -> list[CourseSummary]:
        """Courses Moodle's own dashboard/timeline classification (see
        MoodleSync.sync_course_classification) currently considers
        "inprogress". A course whose classification was never synced is
        NOT included (unknown is not "current" - fail closed), even if it
        might plausibly be ongoing by startdate/enddate alone.

        Filtered in Python, not SQL: a personal Moodle account's course
        count is always small (tens, not thousands), so fetching all of
        them first costs nothing measurable and keeps this trivially
        consistent with get_courses().
        """
        return [c for c in self.get_courses() if c.is_current]

    def get_course(self, course_id: int) -> CourseSummary | None:
        row = moodle_repo.get_course(self._conn, course_id)
        return _row_to_course_summary(row) if row else None

    # ---- Assignments ---------------------------------------------------

    def get_assignments(self, course_id: int | None = None) -> list[AssignmentSummary]:
        now = int(time.time())
        rows = moodle_repo.get_assignments_with_status(self._conn, course_id)
        return [_row_to_assignment_summary(r, now) for r in rows]

    def get_assignment(self, assignment_id: int) -> AssignmentSummary | None:
        row = moodle_repo.get_assignment_with_status(self._conn, assignment_id)
        return _row_to_assignment_summary(row, int(time.time())) if row else None

    def get_pending_assignments(self) -> list[AssignmentSummary]:
        """"Pending" = próxima (due date not yet past) AND known to not be
        submitted. See is_assignment_pending() for the exact, tested rule -
        this method never re-implements the logic, only filters by it, so
        the two can never drift apart."""
        return [a for a in self.get_assignments() if a.is_pending]

    def get_upcoming_assignments(self, days: int = 7) -> list[AssignmentSummary]:
        """Assignments due within the next `days` days, regardless of
        submission status - see is_assignment_upcoming()."""
        now = int(time.time())
        return [a for a in self.get_assignments() if is_assignment_upcoming(a.duedate, now, days)]

    # ---- Calendar --------------------------------------------------------

    def get_calendar_events(self, start: int, end: int) -> list[CalendarEvent]:
        """Already-synced events with timestart in [start, end] - reads
        local data only, never queries Moodle for a fresher answer."""
        rows = moodle_repo.get_calendar_events_in_range(self._conn, start, end)
        return [_row_to_calendar_event(r) for r in rows]

    def get_course_calendar_events(self, course_id: int, start: int, end: int) -> list[CalendarEvent]:
        rows = moodle_repo.get_calendar_events_in_range(self._conn, start, end, course_id=course_id)
        return [_row_to_calendar_event(r) for r in rows]

    # ---- Grades ------------------------------------------------------

    def get_grades(self, course_id: int | None = None) -> list[Grade]:
        """Grades exactly as Moodle provided them (gradereport_user_get_grade_items,
        already synced) - no averages, GPA, or interpretation computed here."""
        if course_id is None:
            rows = moodle_repo.get_all_grades(self._conn)
        else:
            rows = moodle_repo.get_grades(self._conn, course_id)
        return [_row_to_grade(r) for r in rows]

    # ---- Materials / Extraction ------------------------------------------

    def get_course_materials(self, course_id: int) -> list[MaterialSummary]:
        """Every ingested Resource for this course, with its latest
        ResourceVersion and latest ExtractedDocument attempt when they
        exist (LEFT JOIN - a resource Ingestion hasn't produced a version
        for yet, or that hasn't been through Extraction yet, still appears,
        just with those fields as None).

        Metadata only - never loads extracted_text (see get_extracted_document()
        for that, fetched only when actually needed).
        """
        rows = self._conn.execute(_MATERIALS_SQL, (course_id,)).fetchall()
        return [_row_to_material_summary(r) for r in rows]

    def get_resource(self, resource_id: int) -> Resource | None:
        return ingestion_repo.get_resource(self._conn, resource_id)

    def get_extracted_document(
        self, resource_version_id: int, depth: str = _DEFAULT_DEPTH
    ) -> ExtractedDocument | None:
        """The most recent extraction attempt for this ResourceVersion at
        the given depth, successful or not (status/error_reason included) -
        delegates directly to extraction_repository, which already owns
        this exact query; extracted_text lives only here, never copied
        elsewhere.

        depth defaults to "basic" - the only depth Extraction currently
        produces in practice (DEC-058: basic/deep aren't yet
        differentiated).
        """
        return extraction_repo.get_latest_extraction(self._conn, resource_version_id, depth)

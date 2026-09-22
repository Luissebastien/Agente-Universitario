from __future__ import annotations

from dataclasses import dataclass

# Read models new to this layer. Deliberately NOT a 1:1 wrapper around every
# concept: where an existing dataclass already has the right shape (Grade,
# CalendarEvent, Resource, ExtractedDocument), AcademicReadModel returns that
# existing type directly instead of an unnecessary duplicate - see
# read_model.py.


@dataclass(frozen=True)
class CourseSummary:
    """A course as the Read Model exposes it - adds is_current/
    timeline_classification (Moodle's own dashboard classification, see
    MoodleSync.sync_course_classification) on top of the synced Course row.

    is_current is False whenever timeline_classification hasn't been synced
    yet (None) - unknown is never treated as "current" (fail closed).
    """

    id: int
    shortname: str
    fullname: str
    category: int | None
    is_current: bool
    timeline_classification: str | None
    progress: float | None
    startdate: int | None
    enddate: int | None
    last_synced_at: str


@dataclass(frozen=True)
class AssignmentSummary:
    """An assignment together with the authenticated user's own submission
    status, when known. submission_status/grading_status are None when
    Moodle's per-assignment submission status was never synced for this
    assignment (see MoodleSync.sync_assignment_submission_status) - this is
    a real, distinct state from "known not submitted", not an oversight.

    is_pending is computed once, at read time, via
    read_model.is_assignment_pending() - see that function for the exact,
    tested definition of "pending".
    """

    id: int
    course_id: int
    name: str
    duedate: int | None
    allowsubmissionsfromdate: int | None
    cutoffdate: int | None
    grade: float | None
    submission_status: str | None
    grading_status: str | None
    is_pending: bool


@dataclass(frozen=True)
class MaterialSummary:
    """One course material: a Resource (Ingestion's world) joined with its
    latest ResourceVersion and latest ExtractedDocument, when they exist.

    Deliberately metadata-only - never carries extracted_text (fetch that
    separately via get_extracted_document() only when actually needed, per
    the "don't load full documents for a metadata-only query" requirement).
    """

    resource_id: int
    name: str
    course_id: int | None
    section_id: int | None
    module_id: int | None
    source_type: str
    source_url: str
    latest_version_id: int | None
    latest_version_number: int | None
    mimetype: str | None
    size_bytes: int | None
    ingested_at: str | None
    extraction_status: str | None
    extractor_name: str | None
    extracted_at: str | None

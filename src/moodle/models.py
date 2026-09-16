from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: str | None) -> str | None:
    """Reduce short Moodle-authored HTML (e.g. an event note) to plain text.

    Not a general HTML sanitizer - just enough to avoid storing markup for
    the small free-text fields Moodle returns as HTML fragments.
    """
    if value is None:
        return None
    text = _HTML_TAG_RE.sub("", value).strip()
    return text or None


@dataclass(frozen=True)
class MoodleUser:
    """The authenticated Agente U user, as Moodle sees them."""

    id: int
    username: str
    fullname: str

    @classmethod
    def from_site_info(cls, data: dict[str, Any]) -> MoodleUser:
        return cls(
            id=data["userid"],
            username=data.get("username", ""),
            fullname=data.get("fullname", ""),
        )


@dataclass(frozen=True)
class Course:
    id: int
    shortname: str
    fullname: str
    category: int | None
    visible: bool
    progress: float | None
    startdate: int | None
    enddate: int | None

    @classmethod
    def from_moodle(cls, data: dict[str, Any]) -> Course:
        return cls(
            id=data["id"],
            shortname=data.get("shortname", ""),
            fullname=data.get("fullname", ""),
            category=data.get("category"),
            visible=bool(data.get("visible", True)),
            progress=data.get("progress"),
            startdate=data.get("startdate"),
            enddate=data.get("enddate"),
        )


@dataclass(frozen=True)
class CourseSection:
    id: int
    course_id: int
    section_number: int | None
    name: str
    summary: str
    visible: bool

    @classmethod
    def from_moodle(cls, course_id: int, data: dict[str, Any]) -> CourseSection:
        return cls(
            id=data["id"],
            course_id=course_id,
            section_number=data.get("section"),
            name=data.get("name", ""),
            summary=data.get("summary", ""),
            visible=bool(data.get("visible", True)),
        )


@dataclass(frozen=True)
class CourseModule:
    id: int
    course_id: int
    section_id: int
    modname: str
    name: str
    url: str | None
    visible: bool

    @classmethod
    def from_moodle(cls, course_id: int, section_id: int, data: dict[str, Any]) -> CourseModule:
        return cls(
            id=data["id"],
            course_id=course_id,
            section_id=section_id,
            modname=data.get("modname", ""),
            name=data.get("name", ""),
            url=data.get("url"),
            visible=bool(data.get("visible", True)),
        )


@dataclass(frozen=True)
class CourseFile:
    """A single downloadable file inside a course module's 'contents' list.

    Only entries with content type == 'file' should be mapped here; embedded
    external links (type == 'url') are not files and are not represented.
    """

    module_id: int
    filename: str
    filepath: str
    filesize: int
    mimetype: str | None
    fileurl: str
    timemodified: int | None

    @classmethod
    def from_moodle(cls, module_id: int, data: dict[str, Any]) -> CourseFile:
        return cls(
            module_id=module_id,
            filename=data.get("filename", ""),
            filepath=data.get("filepath", "/"),
            filesize=data.get("filesize", 0),
            mimetype=data.get("mimetype"),
            fileurl=data["fileurl"],
            timemodified=data.get("timemodified"),
        )


@dataclass(frozen=True)
class Assignment:
    id: int
    course_id: int
    name: str
    duedate: int | None
    allowsubmissionsfromdate: int | None
    cutoffdate: int | None
    grade: float | None

    @classmethod
    def from_moodle(cls, course_id: int, data: dict[str, Any]) -> Assignment:
        return cls(
            id=data["id"],
            course_id=course_id,
            name=data.get("name", ""),
            duedate=data.get("duedate"),
            allowsubmissionsfromdate=data.get("allowsubmissionsfromdate"),
            cutoffdate=data.get("cutoffdate"),
            grade=data.get("grade"),
        )


@dataclass(frozen=True)
class AssignmentSubmissionStatus:
    """The authenticated user's own submission status for one assignment.

    Kept separate from Assignment on purpose: this is per-user, mutable,
    lower-value state (mod_assign_get_submission_status), while Assignment
    is the shared, mostly-static definition of the activity itself
    (mod_assign_get_assignments). Mixing them would make Assignment's
    identity depend on who is asking.
    """

    assignment_id: int
    submission_status: str | None
    grading_status: str | None
    cansubmit: bool | None
    submitted_at: int | None

    @classmethod
    def from_moodle(cls, assignment_id: int, data: dict[str, Any]) -> AssignmentSubmissionStatus:
        lastattempt = data.get("lastattempt") or {}
        submission = lastattempt.get("submission") or {}
        return cls(
            assignment_id=assignment_id,
            submission_status=submission.get("status"),
            grading_status=lastattempt.get("gradingstatus"),
            cansubmit=lastattempt.get("cansubmit"),
            submitted_at=submission.get("timemodified"),
        )


@dataclass(frozen=True)
class Grade:
    """A single grade item's value for one user, from gradereport_user_get_grade_items.

    `id` is Moodle's own grade_item id (globally unique, not invented here) -
    the natural key for upserts. `cmid` links back to course_modules.id when
    the item corresponds to an activity (itemtype == 'mod'); it is None for
    aggregate rows such as the overall course grade (itemtype == 'course').
    """

    id: int
    course_id: int
    user_id: int
    cmid: int | None
    item_name: str | None
    item_type: str | None
    item_module: str | None
    grade_raw: float | None
    grade_formatted: str | None
    percentage_formatted: str | None

    @classmethod
    def from_moodle(cls, course_id: int, user_id: int, data: dict[str, Any]) -> Grade:
        return cls(
            id=data["id"],
            course_id=course_id,
            user_id=user_id,
            cmid=data.get("cmid"),
            item_name=data.get("itemname"),
            item_type=data.get("itemtype"),
            item_module=data.get("itemmodule"),
            grade_raw=data.get("graderaw"),
            grade_formatted=data.get("gradeformatted"),
            percentage_formatted=data.get("percentageformatted"),
        )


@dataclass(frozen=True)
class CalendarEvent:
    id: int
    course_id: int | None
    name: str
    description: str | None
    eventtype: str
    modulename: str | None
    instance: int | None
    timestart: int
    timesort: int | None
    timeduration: int | None

    @classmethod
    def from_moodle(cls, data: dict[str, Any]) -> CalendarEvent:
        course = data.get("course") or {}
        return cls(
            id=data["id"],
            course_id=course.get("id"),
            name=data.get("name", ""),
            description=_strip_html(data.get("description")),
            eventtype=data.get("eventtype", ""),
            modulename=data.get("modulename"),
            instance=data.get("instance"),
            timestart=data["timestart"],
            timesort=data.get("timesort"),
            timeduration=data.get("timeduration"),
        )

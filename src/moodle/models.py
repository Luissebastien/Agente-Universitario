from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


# -- Models defined for Fase 2 but not yet persisted/synced (see sync.py) -----
# Their shape is already validated against real Moodle responses; wiring them
# into the database is intentionally left for a follow-up task.


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
class Grade:
    course_id: int
    item_name: str | None
    item_type: str | None
    item_module: str | None
    grade_raw: float | None
    grade_formatted: str | None
    percentage_formatted: str | None

    @classmethod
    def from_moodle(cls, course_id: int, data: dict[str, Any]) -> Grade:
        return cls(
            course_id=course_id,
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
    eventtype: str
    modulename: str | None
    instance: int | None
    timestart: int

    @classmethod
    def from_moodle(cls, data: dict[str, Any]) -> CalendarEvent:
        course = data.get("course") or {}
        return cls(
            id=data["id"],
            course_id=course.get("id"),
            name=data.get("name", ""),
            eventtype=data.get("eventtype", ""),
            modulename=data.get("modulename"),
            instance=data.get("instance"),
            timestart=data["timestart"],
        )

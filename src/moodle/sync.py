from __future__ import annotations

import sqlite3

from database import moodle_repository as repo
from moodle.client import MoodleClient
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


class MoodleSync:
    """Pulls data from Moodle via MoodleClient and persists it as normalized rows.

    Stateless with respect to business logic: it only knows how to fetch,
    normalize, and upsert. Deciding *when* to sync belongs to a future
    scheduler, not to this class.
    """

    def __init__(self, client: MoodleClient, conn: sqlite3.Connection) -> None:
        self._client = client
        self._conn = conn

    def sync_profile(self) -> MoodleUser:
        """core_webservice_get_site_info -> moodle_users (our own identity)."""
        site_info = self._client.get_site_info()
        user = MoodleUser.from_site_info(site_info)
        repo.upsert_user(self._conn, user)
        return user

    def sync_courses(self, user_id: int | None = None) -> list[Course]:
        """core_enrol_get_users_courses -> courses."""
        raw_courses = self._client.get_courses(user_id)
        courses = [Course.from_moodle(raw) for raw in raw_courses]
        repo.upsert_courses(self._conn, courses)
        return courses

    def sync_course_contents(self, course_id: int) -> list[CourseSection]:
        """core_course_get_contents -> course_sections, course_modules, course_files."""
        raw_sections = self._client.get_course_contents(course_id)

        sections: list[CourseSection] = []
        modules: list[CourseModule] = []
        files: list[CourseFile] = []

        for raw_section in raw_sections:
            section = CourseSection.from_moodle(course_id, raw_section)
            sections.append(section)

            for raw_module in raw_section.get("modules", []) or []:
                module = CourseModule.from_moodle(course_id, section.id, raw_module)
                modules.append(module)

                for raw_content in raw_module.get("contents", []) or []:
                    if raw_content.get("type") == "file" and raw_content.get("fileurl"):
                        files.append(CourseFile.from_moodle(module.id, raw_content))

        repo.upsert_sections(self._conn, sections)
        repo.upsert_modules(self._conn, modules)
        repo.upsert_files(self._conn, files)
        return sections

    def sync_assignments(self, course_ids: list[int] | None = None) -> list[Assignment]:
        """mod_assign_get_assignments -> assignments, for the user's own courses.

        Only the assignment definitions are fetched here (one bulk call for
        all courses). Per-user submission state is a separate, explicit call
        via sync_assignment_submission_status - not done automatically for
        every assignment, to avoid one Moodle request per assignment.
        """
        if course_ids is None:
            course_ids = [row["id"] for row in repo.get_courses(self._conn)]
        if not course_ids:
            return []

        raw = self._client.call("mod_assign_get_assignments", {"courseids": course_ids})
        assignments = [
            Assignment.from_moodle(raw_course["id"], raw_assignment)
            for raw_course in raw.get("courses", [])
            for raw_assignment in raw_course.get("assignments", []) or []
        ]
        repo.upsert_assignments(self._conn, assignments)
        return assignments

    def sync_assignment_submission_status(self, assignment_id: int) -> AssignmentSubmissionStatus:
        """mod_assign_get_submission_status -> assignment_submission_status, for one assignment.

        Only ever reads the authenticated user's own status (Moodle scopes
        this call to the caller by default); never another user's.
        """
        raw = self._client.call("mod_assign_get_submission_status", {"assignid": assignment_id})
        status = AssignmentSubmissionStatus.from_moodle(assignment_id, raw)
        repo.upsert_assignment_submission_status(self._conn, status)
        return status

    def sync_grades(self, course_id: int, user_id: int | None = None) -> list[Grade]:
        """gradereport_user_get_grade_items -> grades, for one course.

        Deliberately not gradereport_user_get_grades_table, which returns
        rendered HTML rather than structured data.
        """
        if user_id is None:
            user_id = self._client.get_site_info()["userid"]

        raw = self._client.call(
            "gradereport_user_get_grade_items", {"courseid": course_id, "userid": user_id}
        )
        grades = [
            Grade.from_moodle(course_id, user_id, raw_item)
            for raw_usergrade in raw.get("usergrades", [])
            for raw_item in raw_usergrade.get("gradeitems", []) or []
        ]
        repo.upsert_grades(self._conn, grades)
        return grades

    def sync_calendar(self, course_ids: list[int] | None = None) -> list[CalendarEvent]:
        """core_calendar_get_action_events_by_courses -> calendar_events, in bulk."""
        if course_ids is None:
            course_ids = [row["id"] for row in repo.get_courses(self._conn)]
        if not course_ids:
            return []

        raw = self._client.call(
            "core_calendar_get_action_events_by_courses", {"courseids": course_ids}
        )
        events = [
            CalendarEvent.from_moodle(raw_event)
            for group in raw.get("groupedbycourse", [])
            for raw_event in group.get("events", []) or []
        ]
        repo.upsert_calendar_events(self._conn, events)
        return events

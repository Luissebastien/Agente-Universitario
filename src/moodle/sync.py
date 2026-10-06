from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from database import moodle_repository as repo
from moodle.client import MoodleClient
from moodle.exceptions import MoodleAPIError, MoodleAuthenticationError
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


@dataclass(frozen=True)
class SyncReport:
    """What one MoodleSync.sync_changes() run did. Counts only - no academic content."""

    full: bool
    courses: int
    changed_courses: tuple[int, ...]
    detection_warnings: int
    assignments: int
    statuses_refreshed: int
    grade_items: int
    calendar_events: int
    item_warnings: tuple[str, ...] = ()

    @property
    def items_processed(self) -> int:
        return (
            self.courses + self.assignments + self.statuses_refreshed
            + self.grade_items + self.calendar_events
        )

    def summary(self) -> str:
        mode = "full" if self.full else "incremental"
        text = (
            f"{mode}: {len(self.changed_courses)}/{self.courses} course(s) re-synced, "
            f"{self.assignments} assignment(s), {self.statuses_refreshed} status(es), "
            f"{self.grade_items} grade item(s), {self.calendar_events} event(s)"
        )
        if self.detection_warnings:
            text += f", {self.detection_warnings} update-detection warning(s)"
        if self.item_warnings:
            text += f", {len(self.item_warnings)} item warning(s)"
        return text


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

    def sync_course_classification(self) -> dict[int, str]:
        """core_course_get_enrolled_courses_by_timeline_classification ->
        courses.timeline_classification.

        This is Moodle's own dashboard/mobile-app course classification
        ('inprogress' | 'past' | 'future'), not a heuristic derived here from
        startdate/enddate - verified against the real Moodle instance to
        return exactly the same courses as sync_courses(), correctly split
        by classification. A course must already exist in `courses` (i.e.
        sync_courses() has run) for this to have any effect - it only
        updates the classification column, never inserts a course.
        """
        classifications: dict[int, str] = {}
        for classification in ("inprogress", "past", "future"):
            raw_courses = self._client.get_enrolled_courses_by_timeline_classification(classification)
            for raw_course in raw_courses or []:
                classifications[raw_course["id"]] = classification

        for course_id, classification in classifications.items():
            repo.set_course_timeline_classification(self._conn, course_id, classification)
        return classifications

    def sync_changes(self, since: int | None, now: int | None = None) -> SyncReport:
        """One scheduled synchronization cycle - incremental when possible.

        since=None is a full synchronization of every enrolled course (first
        run, Scheduler startup). Otherwise core_course_get_updates_since
        decides which courses' contents to re-fetch. A course counts as
        changed when the API reports any updated module, OR any warning (a
        warning means some module's changes are invisible to the API - fail
        closed), OR its contents were never stored.

        Only entities Moodle returned in THIS run are iterated: rows are never
        deleted, so DB-derived lists would include dropped courses and deleted
        assignments that now fail on every call. A non-authentication API
        error on one assignment's status or one course's grades becomes an
        item warning (stored values left untouched); authentication,
        connection and HTTP errors propagate, failing the whole run so the
        caller's checkpoint does not advance.

        Known API limits (closed research): no deletion signal and no
        intermediate history, so a quiet incremental run is not a global
        reconciliation - only a full run (since=None) re-fetches everything.
        """
        now = int(time.time()) if now is None else now
        user = self.sync_profile()
        courses = self.sync_courses(user.id)
        self.sync_course_classification()
        course_ids = [course.id for course in courses]

        changed: list[int] = []
        detection_warnings = 0
        for course_id in course_ids:
            if since is None or not repo.course_has_contents(self._conn, course_id):
                changed.append(course_id)
                continue
            updates = self._client.get_updates_since(course_id, since)
            warnings = updates.get("warnings") or []
            detection_warnings += len(warnings)
            if updates.get("instances") or warnings:
                changed.append(course_id)

        for course_id in changed:
            self.sync_course_contents(course_id)

        assignments = self.sync_assignments(course_ids)
        returned_ids = {assignment.id for assignment in assignments}
        changed_set = set(changed)
        status_ids = {a.id for a in assignments if a.course_id in changed_set}
        status_ids |= repo.get_assignment_ids_needing_status(self._conn, now) & returned_ids

        item_warnings: list[str] = []
        statuses_refreshed = 0
        for assignment_id in sorted(status_ids):
            try:
                self.sync_assignment_submission_status(assignment_id)
                statuses_refreshed += 1
            except MoodleAuthenticationError:
                raise
            except MoodleAPIError as exc:
                item_warnings.append(f"submission status of assignment {assignment_id}: {exc}")

        grade_items = 0
        for course_id in changed:
            try:
                grade_items += len(self.sync_grades(course_id, user.id))
            except MoodleAuthenticationError:
                raise
            except MoodleAPIError as exc:
                item_warnings.append(f"grades of course {course_id}: {exc}")

        events = self.sync_calendar(course_ids)

        return SyncReport(
            full=since is None,
            courses=len(course_ids),
            changed_courses=tuple(changed),
            detection_warnings=detection_warnings,
            assignments=len(assignments),
            statuses_refreshed=statuses_refreshed,
            grade_items=grade_items,
            calendar_events=len(events),
            item_warnings=tuple(item_warnings),
        )

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

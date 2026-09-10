from __future__ import annotations

import sqlite3

from database import moodle_repository as repo
from moodle.client import MoodleClient
from moodle.models import Course, CourseFile, CourseModule, CourseSection, MoodleUser


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

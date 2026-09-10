from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime, timezone

from moodle.models import Course, CourseFile, CourseModule, CourseSection, MoodleUser


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_user(conn: sqlite3.Connection, user: MoodleUser) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO moodle_users (id, username, fullname, created_at, last_synced_at)
        VALUES (:id, :username, :fullname, :now, :now)
        ON CONFLICT(id) DO UPDATE SET
            username = excluded.username,
            fullname = excluded.fullname,
            last_synced_at = excluded.last_synced_at
        """,
        {**asdict(user), "now": now},
    )
    conn.commit()


def upsert_courses(conn: sqlite3.Connection, courses: Iterable[Course]) -> None:
    now = _now()
    conn.executemany(
        """
        INSERT INTO courses
            (id, shortname, fullname, category, visible, progress, startdate, enddate, created_at, last_synced_at)
        VALUES
            (:id, :shortname, :fullname, :category, :visible, :progress, :startdate, :enddate, :now, :now)
        ON CONFLICT(id) DO UPDATE SET
            shortname = excluded.shortname,
            fullname = excluded.fullname,
            category = excluded.category,
            visible = excluded.visible,
            progress = excluded.progress,
            startdate = excluded.startdate,
            enddate = excluded.enddate,
            last_synced_at = excluded.last_synced_at
        """,
        [{**asdict(course), "now": now} for course in courses],
    )
    conn.commit()


def upsert_sections(conn: sqlite3.Connection, sections: Iterable[CourseSection]) -> None:
    now = _now()
    conn.executemany(
        """
        INSERT INTO course_sections
            (id, course_id, section_number, name, summary, visible, created_at, last_synced_at)
        VALUES
            (:id, :course_id, :section_number, :name, :summary, :visible, :now, :now)
        ON CONFLICT(id) DO UPDATE SET
            course_id = excluded.course_id,
            section_number = excluded.section_number,
            name = excluded.name,
            summary = excluded.summary,
            visible = excluded.visible,
            last_synced_at = excluded.last_synced_at
        """,
        [{**asdict(section), "now": now} for section in sections],
    )
    conn.commit()


def upsert_modules(conn: sqlite3.Connection, modules: Iterable[CourseModule]) -> None:
    now = _now()
    conn.executemany(
        """
        INSERT INTO course_modules
            (id, course_id, section_id, modname, name, url, visible, created_at, last_synced_at)
        VALUES
            (:id, :course_id, :section_id, :modname, :name, :url, :visible, :now, :now)
        ON CONFLICT(id) DO UPDATE SET
            course_id = excluded.course_id,
            section_id = excluded.section_id,
            modname = excluded.modname,
            name = excluded.name,
            url = excluded.url,
            visible = excluded.visible,
            last_synced_at = excluded.last_synced_at
        """,
        [{**asdict(module), "now": now} for module in modules],
    )
    conn.commit()


def upsert_files(conn: sqlite3.Connection, files: Iterable[CourseFile]) -> None:
    now = _now()
    conn.executemany(
        """
        INSERT INTO course_files
            (module_id, filename, filepath, filesize, mimetype, fileurl, timemodified, created_at, last_synced_at)
        VALUES
            (:module_id, :filename, :filepath, :filesize, :mimetype, :fileurl, :timemodified, :now, :now)
        ON CONFLICT(fileurl) DO UPDATE SET
            module_id = excluded.module_id,
            filename = excluded.filename,
            filepath = excluded.filepath,
            filesize = excluded.filesize,
            mimetype = excluded.mimetype,
            timemodified = excluded.timemodified,
            last_synced_at = excluded.last_synced_at
        """,
        [{**asdict(file), "now": now} for file in files],
    )
    conn.commit()


def get_courses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM courses ORDER BY fullname").fetchall()


def get_course_modules(conn: sqlite3.Connection, course_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM course_modules WHERE course_id = ? ORDER BY id", (course_id,)
    ).fetchall()

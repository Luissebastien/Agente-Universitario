from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime, timezone

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


def upsert_assignments(conn: sqlite3.Connection, assignments: Iterable[Assignment]) -> None:
    now = _now()
    conn.executemany(
        """
        INSERT INTO assignments
            (id, course_id, name, duedate, allowsubmissionsfromdate, cutoffdate, grade, created_at, last_synced_at)
        VALUES
            (:id, :course_id, :name, :duedate, :allowsubmissionsfromdate, :cutoffdate, :grade, :now, :now)
        ON CONFLICT(id) DO UPDATE SET
            course_id = excluded.course_id,
            name = excluded.name,
            duedate = excluded.duedate,
            allowsubmissionsfromdate = excluded.allowsubmissionsfromdate,
            cutoffdate = excluded.cutoffdate,
            grade = excluded.grade,
            last_synced_at = excluded.last_synced_at
        """,
        [{**asdict(a), "now": now} for a in assignments],
    )
    conn.commit()


def get_assignments(conn: sqlite3.Connection, course_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM assignments WHERE course_id = ? ORDER BY duedate", (course_id,)
    ).fetchall()


def upsert_assignment_submission_status(
    conn: sqlite3.Connection, status: AssignmentSubmissionStatus
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO assignment_submission_status
            (assignment_id, submission_status, grading_status, cansubmit, submitted_at, created_at, last_synced_at)
        VALUES
            (:assignment_id, :submission_status, :grading_status, :cansubmit, :submitted_at, :now, :now)
        ON CONFLICT(assignment_id) DO UPDATE SET
            submission_status = excluded.submission_status,
            grading_status = excluded.grading_status,
            cansubmit = excluded.cansubmit,
            submitted_at = excluded.submitted_at,
            last_synced_at = excluded.last_synced_at
        """,
        {**asdict(status), "now": now},
    )
    conn.commit()


def get_assignment_submission_status(
    conn: sqlite3.Connection, assignment_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM assignment_submission_status WHERE assignment_id = ?", (assignment_id,)
    ).fetchone()


def upsert_grades(conn: sqlite3.Connection, grades: Iterable[Grade]) -> None:
    now = _now()
    conn.executemany(
        """
        INSERT INTO grades
            (id, course_id, user_id, cmid, item_name, item_type, item_module,
             grade_raw, grade_formatted, percentage_formatted, created_at, last_synced_at)
        VALUES
            (:id, :course_id, :user_id, :cmid, :item_name, :item_type, :item_module,
             :grade_raw, :grade_formatted, :percentage_formatted, :now, :now)
        ON CONFLICT(id) DO UPDATE SET
            course_id = excluded.course_id,
            user_id = excluded.user_id,
            cmid = excluded.cmid,
            item_name = excluded.item_name,
            item_type = excluded.item_type,
            item_module = excluded.item_module,
            grade_raw = excluded.grade_raw,
            grade_formatted = excluded.grade_formatted,
            percentage_formatted = excluded.percentage_formatted,
            last_synced_at = excluded.last_synced_at
        """,
        [{**asdict(g), "now": now} for g in grades],
    )
    conn.commit()


def get_grades(conn: sqlite3.Connection, course_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM grades WHERE course_id = ? ORDER BY item_name", (course_id,)
    ).fetchall()


def upsert_calendar_events(conn: sqlite3.Connection, events: Iterable[CalendarEvent]) -> None:
    now = _now()
    conn.executemany(
        """
        INSERT INTO calendar_events
            (id, course_id, name, description, eventtype, modulename, instance,
             timestart, timesort, timeduration, created_at, last_synced_at)
        VALUES
            (:id, :course_id, :name, :description, :eventtype, :modulename, :instance,
             :timestart, :timesort, :timeduration, :now, :now)
        ON CONFLICT(id) DO UPDATE SET
            course_id = excluded.course_id,
            name = excluded.name,
            description = excluded.description,
            eventtype = excluded.eventtype,
            modulename = excluded.modulename,
            instance = excluded.instance,
            timestart = excluded.timestart,
            timesort = excluded.timesort,
            timeduration = excluded.timeduration,
            last_synced_at = excluded.last_synced_at
        """,
        [{**asdict(e), "now": now} for e in events],
    )
    conn.commit()


def get_calendar_events(
    conn: sqlite3.Connection, course_id: int | None = None
) -> list[sqlite3.Row]:
    if course_id is None:
        return conn.execute("SELECT * FROM calendar_events ORDER BY timestart").fetchall()
    return conn.execute(
        "SELECT * FROM calendar_events WHERE course_id = ? ORDER BY timestart", (course_id,)
    ).fetchall()

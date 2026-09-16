from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS moodle_users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    fullname TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    shortname TEXT NOT NULL,
    fullname TEXT NOT NULL,
    category INTEGER,
    visible INTEGER NOT NULL,
    progress REAL,
    startdate INTEGER,
    enddate INTEGER,
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS course_sections (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    section_number INTEGER,
    name TEXT,
    summary TEXT,
    visible INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS course_modules (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    section_id INTEGER NOT NULL REFERENCES course_sections(id),
    modname TEXT NOT NULL,
    name TEXT,
    url TEXT,
    visible INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS course_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id INTEGER NOT NULL REFERENCES course_modules(id),
    filename TEXT NOT NULL,
    filepath TEXT,
    filesize INTEGER,
    mimetype TEXT,
    fileurl TEXT NOT NULL UNIQUE,
    timemodified INTEGER,
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    name TEXT NOT NULL,
    duedate INTEGER,
    allowsubmissionsfromdate INTEGER,
    cutoffdate INTEGER,
    grade REAL,
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignment_submission_status (
    assignment_id INTEGER PRIMARY KEY REFERENCES assignments(id),
    submission_status TEXT,
    grading_status TEXT,
    cansubmit INTEGER,
    submitted_at INTEGER,
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grades (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    user_id INTEGER NOT NULL REFERENCES moodle_users(id),
    cmid INTEGER,
    item_name TEXT,
    item_type TEXT,
    item_module TEXT,
    grade_raw REAL,
    grade_formatted TEXT,
    percentage_formatted TEXT,
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY,
    course_id INTEGER REFERENCES courses(id),
    name TEXT NOT NULL,
    description TEXT,
    eventtype TEXT NOT NULL,
    modulename TEXT,
    instance INTEGER,
    timestart INTEGER NOT NULL,
    timesort INTEGER,
    timeduration INTEGER,
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    source_type TEXT NOT NULL,
    external_reference TEXT NOT NULL,
    course_id INTEGER REFERENCES courses(id),
    section_id INTEGER REFERENCES course_sections(id),
    module_id INTEGER REFERENCES course_modules(id),
    name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    UNIQUE (origin, source_type, external_reference)
);

CREATE TABLE IF NOT EXISTS resource_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL REFERENCES resources(id),
    version_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mimetype TEXT,
    ingested_at TEXT NOT NULL,
    UNIQUE (resource_id, version_number)
);

CREATE TABLE IF NOT EXISTS extracted_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_version_id INTEGER NOT NULL REFERENCES resource_versions(id),
    depth TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    status TEXT NOT NULL,
    error_reason TEXT,
    extracted_text TEXT,
    metadata TEXT,
    extracted_at TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if necessary) the local SQLite database and ensure the schema exists.

    db_path may be ":memory:" for tests. Callers own the connection's lifecycle.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn

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

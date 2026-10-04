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
    timeline_classification TEXT,
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

-- Moodle's own timemodified of the original, as last confirmed by a
-- *successful* ingest() (including an unchanged-content no-op). Lets
-- Ingestion tell "Moodle changed this file since we last checked it" by
-- comparing Moodle's clock with Moodle's clock - never with local time.
CREATE TABLE IF NOT EXISTS ingestion_source_checks (
    resource_id INTEGER PRIMARY KEY REFERENCES resources(id),
    source_timemodified INTEGER,
    checked_at TEXT NOT NULL
);

-- Resources deliberately NOT ingested, and why. Today the only reason is
-- 'oversized': a file over the per-download limit is never read, so there is
-- no ResourceVersion to record the decision against. Both the size seen and
-- the limit in force are stored so the limit can be retuned later from real
-- data, and so old rows still explain themselves once it changes.
-- One row per resource (the latest decision), keyed to the exact source
-- version evaluated: if Moodle reports a new timemodified the row no longer
-- matches and the resource is offered for ingestion again.
CREATE TABLE IF NOT EXISTS ingestion_deferrals (
    resource_id INTEGER PRIMARY KEY REFERENCES resources(id),
    reason TEXT NOT NULL,
    size_bytes INTEGER,
    limit_bytes INTEGER NOT NULL,
    source_timemodified INTEGER,
    deferred_at TEXT NOT NULL
);

-- Scheduler MVP (.ai/SCHEDULER-MVP-RULES.md). One row per job attempt.
CREATE TABLE IF NOT EXISTS scheduler_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    trigger TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_seconds REAL,
    items_processed INTEGER,
    detail TEXT,
    error TEXT
);

-- Manual job requests from another process (`python -m scheduler --job`)
-- for the running scheduler to pick up. Rows are deleted once handled.
CREATE TABLE IF NOT EXISTS scheduler_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    requested_at TEXT NOT NULL
);

-- Notification deduplication: identity is (type, subject, scheduled_for),
-- e.g. ('assignment_due_soon', 'assignment:104926', <deadline>).
CREATE TABLE IF NOT EXISTS notifications_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_type TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    UNIQUE (notification_type, subject_key, scheduled_for)
);

-- Read Model query support. Each index below backs a specific query the
-- Read Model actually runs (see src/read_model/) - not a blanket "index
-- everything". Columns already covered by a PRIMARY KEY or UNIQUE
-- constraint (e.g. resource_versions(resource_id, version_number)) are not
-- re-indexed here.
CREATE INDEX IF NOT EXISTS idx_assignments_course_id ON assignments(course_id);
CREATE INDEX IF NOT EXISTS idx_grades_course_id ON grades(course_id);
CREATE INDEX IF NOT EXISTS idx_calendar_events_course_id ON calendar_events(course_id);
CREATE INDEX IF NOT EXISTS idx_calendar_events_timestart ON calendar_events(timestart);
CREATE INDEX IF NOT EXISTS idx_resources_course_id ON resources(course_id);
CREATE INDEX IF NOT EXISTS idx_extracted_documents_resource_version_id
    ON extracted_documents(resource_version_id);
CREATE INDEX IF NOT EXISTS idx_scheduler_executions_job_status
    ON scheduler_executions(job_name, status);
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

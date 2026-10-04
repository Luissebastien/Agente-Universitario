from __future__ import annotations

import abc
import sqlite3
from collections.abc import Iterable

from ingestion.models import ResourceDescriptor


class ResourceSourceAdapter(abc.ABC):
    """Translates rows already synced by MoodleSync into ResourceDescriptor.

    Read-only against the existing schema. Never re-implements sync logic,
    never talks to Moodle directly - MoodleSync remains the only writer of
    course_files/course_modules.
    """

    @abc.abstractmethod
    def discover(self, conn: sqlite3.Connection) -> Iterable[ResourceDescriptor]: ...


# MoodleSync never deletes rows: a file/module Moodle no longer returns (deleted,
# hidden, or renamed under a new fileurl) keeps its old row. sync_course_contents
# upserts sections, then modules, then files, so every row Moodle returned in a
# course's latest contents sync is stamped at or after that sync's sections.
# Rows older than the course's newest section stamp are therefore stale and are
# not offered to Ingestion. Courses never content-synced have no sections, the
# comparison is NULL, and nothing is filtered.
_CURRENT_ROW_FILTER = """
    NOT ({alias}.last_synced_at < (
        SELECT MAX(s.last_synced_at) FROM course_sections s WHERE s.course_id = cm.course_id
    ))
"""


class MoodleFileSourceAdapter(ResourceSourceAdapter):
    """Candidate resources from course_files (real downloadable files) that
    Moodle still returned in the course's latest contents sync."""

    def discover(self, conn: sqlite3.Connection) -> Iterable[ResourceDescriptor]:
        rows = conn.execute(
            f"""
            SELECT
                cf.fileurl AS fileurl,
                cf.filename AS filename,
                cf.mimetype AS mimetype,
                cf.timemodified AS timemodified,
                cf.module_id AS module_id,
                cm.course_id AS course_id,
                cm.section_id AS section_id
            FROM course_files cf
            JOIN course_modules cm ON cm.id = cf.module_id
            WHERE {_CURRENT_ROW_FILTER.format(alias="cf")}
            ORDER BY cf.id
            """
        ).fetchall()

        for row in rows:
            yield ResourceDescriptor(
                origin="moodle",
                source_type="file",
                external_reference=row["fileurl"],
                course_id=row["course_id"],
                section_id=row["section_id"],
                module_id=row["module_id"],
                name=row["filename"],
                source_url=row["fileurl"],
                mimetype=row["mimetype"],
                source_timemodified=row["timemodified"],
            )


class MoodleUrlSourceAdapter(ResourceSourceAdapter):
    """Candidate resources from course_modules where modname == 'url'.

    A URL is not assumed to be a downloadable file - it is preserved as a
    reference. Whether/how its target is ever fetched is a decision for a
    later phase (Extraction), not this adapter.
    """

    def discover(self, conn: sqlite3.Connection) -> Iterable[ResourceDescriptor]:
        rows = conn.execute(
            f"""
            SELECT cm.id, cm.course_id, cm.section_id, cm.name, cm.url
            FROM course_modules cm
            WHERE cm.modname = 'url' AND cm.url IS NOT NULL AND cm.url != ''
              AND {_CURRENT_ROW_FILTER.format(alias="cm")}
            ORDER BY cm.id
            """
        ).fetchall()

        for row in rows:
            yield ResourceDescriptor(
                origin="moodle",
                source_type="url",
                external_reference=str(row["id"]),
                course_id=row["course_id"],
                section_id=row["section_id"],
                module_id=row["id"],
                name=row["name"] or row["url"],
                source_url=row["url"],
                mimetype=None,
            )

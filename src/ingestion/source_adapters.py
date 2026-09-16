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


class MoodleFileSourceAdapter(ResourceSourceAdapter):
    """Candidate resources from course_files (real downloadable files)."""

    def discover(self, conn: sqlite3.Connection) -> Iterable[ResourceDescriptor]:
        rows = conn.execute(
            """
            SELECT
                cf.fileurl AS fileurl,
                cf.filename AS filename,
                cf.mimetype AS mimetype,
                cf.module_id AS module_id,
                cm.course_id AS course_id,
                cm.section_id AS section_id
            FROM course_files cf
            JOIN course_modules cm ON cm.id = cf.module_id
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
            )


class MoodleUrlSourceAdapter(ResourceSourceAdapter):
    """Candidate resources from course_modules where modname == 'url'.

    A URL is not assumed to be a downloadable file - it is preserved as a
    reference. Whether/how its target is ever fetched is a decision for a
    later phase (Extraction), not this adapter.
    """

    def discover(self, conn: sqlite3.Connection) -> Iterable[ResourceDescriptor]:
        rows = conn.execute(
            """
            SELECT id, course_id, section_id, name, url
            FROM course_modules
            WHERE modname = 'url' AND url IS NOT NULL AND url != ''
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

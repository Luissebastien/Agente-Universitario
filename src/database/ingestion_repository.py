from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ingestion.models import Resource, ResourceDescriptor, ResourceVersion


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_resource(row: sqlite3.Row) -> Resource:
    return Resource(
        id=row["id"],
        origin=row["origin"],
        source_type=row["source_type"],
        external_reference=row["external_reference"],
        course_id=row["course_id"],
        section_id=row["section_id"],
        module_id=row["module_id"],
        name=row["name"],
        source_url=row["source_url"],
    )


def _row_to_version(row: sqlite3.Row) -> ResourceVersion:
    return ResourceVersion(
        id=row["id"],
        resource_id=row["resource_id"],
        version_number=row["version_number"],
        content_hash=row["content_hash"],
        storage_ref=row["storage_ref"],
        size_bytes=row["size_bytes"],
        mimetype=row["mimetype"],
        ingested_at=row["ingested_at"],
    )


def upsert_resource(conn: sqlite3.Connection, descriptor: ResourceDescriptor) -> Resource:
    """Get-or-create the stable Resource identified by (origin, source_type, external_reference).

    Provenance fields (course/section/module/name/source_url) are refreshed on
    conflict, matching the same pattern used for Moodle-synced entities -
    identity stays stable, descriptive fields track the latest known state.
    """
    now = _now()
    conn.execute(
        """
        INSERT INTO resources
            (origin, source_type, external_reference, course_id, section_id, module_id,
             name, source_url, created_at, last_synced_at)
        VALUES
            (:origin, :source_type, :external_reference, :course_id, :section_id, :module_id,
             :name, :source_url, :now, :now)
        ON CONFLICT(origin, source_type, external_reference) DO UPDATE SET
            course_id = excluded.course_id,
            section_id = excluded.section_id,
            module_id = excluded.module_id,
            name = excluded.name,
            source_url = excluded.source_url,
            last_synced_at = excluded.last_synced_at
        """,
        {
            "origin": descriptor.origin,
            "source_type": descriptor.source_type,
            "external_reference": descriptor.external_reference,
            "course_id": descriptor.course_id,
            "section_id": descriptor.section_id,
            "module_id": descriptor.module_id,
            "name": descriptor.name,
            "source_url": descriptor.source_url,
            "now": now,
        },
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT * FROM resources
        WHERE origin = ? AND source_type = ? AND external_reference = ?
        """,
        (descriptor.origin, descriptor.source_type, descriptor.external_reference),
    ).fetchone()
    return _row_to_resource(row)


def find_resource_state(
    conn: sqlite3.Connection, origin: str, source_type: str, external_reference: str
) -> tuple[int, str] | None:
    """(resource_id, last_synced_at) for an identity, without creating it.

    last_synced_at is stamped by upsert_resource at the start of every
    ingest() attempt - failed ones included - so it doubles as "time of the
    last ingestion attempt" for fair ordering of pending work.
    """
    row = conn.execute(
        """
        SELECT id, last_synced_at FROM resources
        WHERE origin = ? AND source_type = ? AND external_reference = ?
        """,
        (origin, source_type, external_reference),
    ).fetchone()
    return (row["id"], row["last_synced_at"]) if row else None


def get_source_check(conn: sqlite3.Connection, resource_id: int) -> tuple[bool, int | None]:
    """(checked?, source_timemodified confirmed by the last successful ingest)."""
    row = conn.execute(
        "SELECT source_timemodified FROM ingestion_source_checks WHERE resource_id = ?",
        (resource_id,),
    ).fetchone()
    return (True, row["source_timemodified"]) if row else (False, None)


def record_source_check(
    conn: sqlite3.Connection, resource_id: int, source_timemodified: int | None
) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_source_checks (resource_id, source_timemodified, checked_at)
        VALUES (?, ?, ?)
        ON CONFLICT(resource_id) DO UPDATE SET
            source_timemodified = excluded.source_timemodified,
            checked_at = excluded.checked_at
        """,
        (resource_id, source_timemodified, _now()),
    )
    conn.commit()


def get_resource(conn: sqlite3.Connection, resource_id: int) -> Resource | None:
    row = conn.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()
    return _row_to_resource(row) if row else None


def get_resource_version(conn: sqlite3.Connection, version_id: int) -> ResourceVersion | None:
    row = conn.execute(
        "SELECT * FROM resource_versions WHERE id = ?", (version_id,)
    ).fetchone()
    return _row_to_version(row) if row else None


def get_latest_version(conn: sqlite3.Connection, resource_id: int) -> ResourceVersion | None:
    row = conn.execute(
        """
        SELECT * FROM resource_versions
        WHERE resource_id = ?
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (resource_id,),
    ).fetchone()
    return _row_to_version(row) if row else None


def get_resource_versions(conn: sqlite3.Connection, resource_id: int) -> list[ResourceVersion]:
    rows = conn.execute(
        "SELECT * FROM resource_versions WHERE resource_id = ? ORDER BY version_number",
        (resource_id,),
    ).fetchall()
    return [_row_to_version(row) for row in rows]


def insert_resource_version(conn: sqlite3.Connection, version: ResourceVersion) -> ResourceVersion:
    """Insert a new, immutable ResourceVersion row. Never updates an existing one."""
    cursor = conn.execute(
        """
        INSERT INTO resource_versions
            (resource_id, version_number, content_hash, storage_ref, size_bytes, mimetype, ingested_at)
        VALUES
            (:resource_id, :version_number, :content_hash, :storage_ref, :size_bytes, :mimetype, :ingested_at)
        """,
        {
            "resource_id": version.resource_id,
            "version_number": version.version_number,
            "content_hash": version.content_hash,
            "storage_ref": version.storage_ref,
            "size_bytes": version.size_bytes,
            "mimetype": version.mimetype,
            "ingested_at": version.ingested_at,
        },
    )
    conn.commit()
    new_id = cursor.lastrowid
    row = conn.execute("SELECT * FROM resource_versions WHERE id = ?", (new_id,)).fetchone()
    return _row_to_version(row)

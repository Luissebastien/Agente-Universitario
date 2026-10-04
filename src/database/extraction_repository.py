from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from extraction.models import ExtractedDocument


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_document(row: sqlite3.Row) -> ExtractedDocument:
    return ExtractedDocument(
        id=row["id"],
        resource_version_id=row["resource_version_id"],
        depth=row["depth"],
        extractor_name=row["extractor_name"],
        extractor_version=row["extractor_version"],
        status=row["status"],
        error_reason=row["error_reason"],
        extracted_text=row["extracted_text"],
        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        extracted_at=row["extracted_at"],
    )


def insert_extracted_document(conn: sqlite3.Connection, doc: ExtractedDocument) -> ExtractedDocument:
    """Insert a new extraction attempt. Never updates an existing row (DEC-038: append-only)."""
    cursor = conn.execute(
        """
        INSERT INTO extracted_documents
            (resource_version_id, depth, extractor_name, extractor_version,
             status, error_reason, extracted_text, metadata, extracted_at)
        VALUES
            (:resource_version_id, :depth, :extractor_name, :extractor_version,
             :status, :error_reason, :extracted_text, :metadata, :extracted_at)
        """,
        {
            "resource_version_id": doc.resource_version_id,
            "depth": doc.depth,
            "extractor_name": doc.extractor_name,
            "extractor_version": doc.extractor_version,
            "status": doc.status,
            "error_reason": doc.error_reason,
            "extracted_text": doc.extracted_text,
            "metadata": json.dumps(doc.metadata) if doc.metadata else None,
            "extracted_at": doc.extracted_at or _now(),
        },
    )
    conn.commit()
    new_id = cursor.lastrowid
    row = conn.execute("SELECT * FROM extracted_documents WHERE id = ?", (new_id,)).fetchone()
    return _row_to_document(row)


def get_extracted_documents(
    conn: sqlite3.Connection, resource_version_id: int
) -> list[ExtractedDocument]:
    """All extraction attempts for a ResourceVersion, oldest first."""
    rows = conn.execute(
        "SELECT * FROM extracted_documents WHERE resource_version_id = ? ORDER BY id",
        (resource_version_id,),
    ).fetchall()
    return [_row_to_document(row) for row in rows]


def get_pending_version_ids(
    conn: sqlite3.Connection,
    depth: str,
    max_failed_attempts: int,
    unsupported_extractor_name: str,
) -> list[int]:
    """Current (latest-version) ResourceVersions still needing extraction at `depth`.

    Historical versions are never returned. A version stops being pending
    once it has a 'done' attempt, an explicit unsupported-format attempt, or
    max_failed_attempts failed attempts - so transient failures are retried
    a bounded number of times and permanent ones never loop forever.
    Never-attempted versions come first.
    """
    rows = conn.execute(
        """
        SELECT rv.id,
               (SELECT COUNT(*) FROM extracted_documents f
                WHERE f.resource_version_id = rv.id AND f.depth = :depth
                  AND f.status = 'failed') AS failed_count
        FROM resource_versions rv
        WHERE rv.version_number = (
                SELECT MAX(v2.version_number) FROM resource_versions v2
                WHERE v2.resource_id = rv.resource_id)
          AND NOT EXISTS (
                SELECT 1 FROM extracted_documents ed
                WHERE ed.resource_version_id = rv.id AND ed.depth = :depth
                  AND (ed.status = 'done' OR ed.extractor_name = :unsupported))
          AND (SELECT COUNT(*) FROM extracted_documents f
               WHERE f.resource_version_id = rv.id AND f.depth = :depth
                 AND f.status = 'failed') < :max_failed
        ORDER BY failed_count, rv.id
        """,
        {
            "depth": depth,
            "unsupported": unsupported_extractor_name,
            "max_failed": max_failed_attempts,
        },
    ).fetchall()
    return [row["id"] for row in rows]


def get_latest_extraction(
    conn: sqlite3.Connection, resource_version_id: int, depth: str
) -> ExtractedDocument | None:
    """Most recent attempt for (resource_version_id, depth), regardless of status."""
    row = conn.execute(
        """
        SELECT * FROM extracted_documents
        WHERE resource_version_id = ? AND depth = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (resource_version_id, depth),
    ).fetchone()
    return _row_to_document(row) if row else None

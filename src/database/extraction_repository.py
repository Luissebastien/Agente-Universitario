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

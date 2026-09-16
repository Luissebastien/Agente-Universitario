from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from database import extraction_repository as extraction_repo
from database import ingestion_repository as ingestion_repo
from extraction.extractors import DEFAULT_EXTRACTORS, Extractor
from extraction.models import ExtractedDocument
from ingestion.storage import Storage, StorageError

_VALID_DEPTHS = {"basic", "deep"}
_UNSUPPORTED_EXTRACTOR_NAME = "unsupported"
_UNSUPPORTED_EXTRACTOR_VERSION = "n/a"


class ExtractionError(Exception):
    """Raised only for an invalid request (unknown ResourceVersion, bad depth).

    Never raised for a content/format problem while processing a real,
    known ResourceVersion - those are recorded as a "failed" ExtractedDocument
    instead, per DEC-048: failures must be explicit, isolated, auditable, and
    retryable, and must never block unrelated resources or hide the original.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Extraction:
    """Deterministically converts a preserved ResourceVersion into an ExtractedDocument.

    Does not interpret meaning, call an LLM, or decide relevance - see
    src/extraction/extractors.py for the mechanical format converters this
    phase supports. Unsupported formats are recorded as an explicit failed
    attempt rather than silently skipped or guessed at.

    Every extract() call inserts a new ExtractedDocument row (append-only
    audit trail per DEC-038), even when re-run against the same version/depth.
    """

    def __init__(
        self,
        storage: Storage,
        conn: sqlite3.Connection,
        extractors: list[Extractor] | None = None,
    ) -> None:
        self._storage = storage
        self._conn = conn
        self._extractors = extractors if extractors is not None else DEFAULT_EXTRACTORS

    def extract(self, resource_version_id: int, depth: str = "basic") -> ExtractedDocument:
        if depth not in _VALID_DEPTHS:
            raise ExtractionError(f"Invalid depth: {depth!r} (expected one of {_VALID_DEPTHS})")

        version = ingestion_repo.get_resource_version(self._conn, resource_version_id)
        if version is None:
            raise ExtractionError(f"No such ResourceVersion: {resource_version_id}")

        resource = ingestion_repo.get_resource(self._conn, version.resource_id)
        extension = Path(resource.name).suffix.lower() if resource else ""

        try:
            content = self._storage.retrieve(version.storage_ref)
        except StorageError as exc:
            return self._record_failure(
                version.id, depth, f"could not retrieve stored content: {exc}"
            )

        extractor = self._select_extractor(version.mimetype, extension)
        if extractor is None:
            return self._record_failure(
                version.id,
                depth,
                f"unsupported format (mimetype={version.mimetype!r}, extension={extension!r})",
                extractor_name=_UNSUPPORTED_EXTRACTOR_NAME,
                extractor_version=_UNSUPPORTED_EXTRACTOR_VERSION,
                metadata={"mimetype": version.mimetype, "size_bytes": version.size_bytes},
            )

        try:
            result = extractor.extract(content)
        except Exception as exc:  # noqa: BLE001 - any decode/parse failure degrades safely
            return self._record_failure(
                version.id,
                depth,
                f"{type(exc).__name__}: {exc}",
                extractor_name=extractor.name,
                extractor_version=extractor.version,
            )

        doc = ExtractedDocument(
            id=None,
            resource_version_id=version.id,
            depth=depth,
            extractor_name=extractor.name,
            extractor_version=extractor.version,
            status="done",
            error_reason=None,
            extracted_text=result.text,
            metadata=result.metadata,
            extracted_at=_now(),
        )
        return extraction_repo.insert_extracted_document(self._conn, doc)

    def _select_extractor(self, mimetype: str | None, extension: str) -> Extractor | None:
        for extractor in self._extractors:
            if extractor.supports(mimetype, extension):
                return extractor
        return None

    def _record_failure(
        self,
        resource_version_id: int,
        depth: str,
        error_reason: str,
        extractor_name: str = "none",
        extractor_version: str = "n/a",
        metadata: dict | None = None,
    ) -> ExtractedDocument:
        doc = ExtractedDocument(
            id=None,
            resource_version_id=resource_version_id,
            depth=depth,
            extractor_name=extractor_name,
            extractor_version=extractor_version,
            status="failed",
            error_reason=error_reason,
            extracted_text=None,
            metadata=metadata or {},
            extracted_at=_now(),
        )
        return extraction_repo.insert_extracted_document(self._conn, doc)

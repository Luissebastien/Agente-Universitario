from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from database import extraction_repository as extraction_repo
from database import ingestion_repository as ingestion_repo
from extraction.extractors import ExtractionInterrupted, Extractor, build_default_extractors
from extraction.models import ExtractedDocument
from extraction.ocr import OcrEngine
from ingestion.storage import Storage, StorageError

logger = logging.getLogger(__name__)

_VALID_DEPTHS = {"basic", "deep"}
_UNSUPPORTED_EXTRACTOR_NAME = "unsupported"
_UNSUPPORTED_EXTRACTOR_VERSION = "n/a"

# Bounded automatic retry of failed (non-"unsupported") attempts: transient
# failures (first-time OCR model download, a storage hiccup) get retried,
# deterministic ones stop instead of producing an append-only row forever.
MAX_FAILED_ATTEMPTS = 3


@dataclass(frozen=True)
class ExtractionBatchResult:
    pending: int
    attempted: int
    done: int
    failed: int
    first_error: str | None = None

    @property
    def remaining(self) -> int:
        return self.pending - self.attempted


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

    depth ("basic"/"deep") is recorded but does not currently change which
    extractor runs or how - both depths use identical extraction for every
    format in this phase. Real depth differentiation (e.g. deeper OCR,
    structural parsing) is deliberately deferred; see .ai/decisions.md.

    ocr_engine is optional and defaults to None (no OCR available - PDFs use
    native extraction only, images become unsupported). Extraction never
    imports a specific OCR library itself; see extraction/ocr.py.
    """

    def __init__(
        self,
        storage: Storage,
        conn: sqlite3.Connection,
        ocr_engine: OcrEngine | None = None,
        extractors: list[Extractor] | None = None,
    ) -> None:
        self._storage = storage
        self._conn = conn
        self._extractors = extractors if extractors is not None else build_default_extractors(ocr_engine)

    def extract(
        self,
        resource_version_id: int,
        depth: str = "basic",
        should_stop: Callable[[], bool] = lambda: False,
    ) -> ExtractedDocument:
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
            result = (
                extractor.extract(content, should_stop)
                if extractor.interruptible
                else extractor.extract(content)
            )
        except ExtractionInterrupted:
            # Deliberately recorded nowhere: the version must stay pending and
            # intact, so the next run retries the whole document. See
            # ExtractionInterrupted's docstring.
            raise
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

    def pending_version_ids(self, depth: str = "basic") -> list[int]:
        """Current (latest) versions still needing extraction at `depth`, never
        historical ones - see extraction_repository.get_pending_version_ids."""
        return extraction_repo.get_pending_version_ids(
            self._conn, depth, MAX_FAILED_ATTEMPTS, _UNSUPPORTED_EXTRACTOR_NAME
        )

    def extract_pending(
        self,
        depth: str,
        max_items: int,
        max_seconds: float,
        should_stop: Callable[[], bool] = lambda: False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> ExtractionBatchResult:
        """extract() pending current versions within a processing budget.

        Stops between items at max_items, max_seconds or should_stop(); the
        rest stays pending for the next run. A long per-page OCR also honours
        should_stop() *within* an item: that raises ExtractionInterrupted,
        which records nothing and leaves the version pending and intact.
        There is no page or time cap per document - a scanned book is
        legitimate material and is allowed to finish. Content/format failures are
        already recorded by extract() as 'failed' rows. Any other unexpected
        per-item exception (e.g. the extracted text cannot be stored) is also
        recorded as a failed attempt so it counts toward the bounded retry and
        never blocks the following versions; only database errors propagate.
        """
        version_ids = self.pending_version_ids(depth)
        started = monotonic()
        attempted = done = failed = 0
        first_error: str | None = None

        for version_id in version_ids:
            if attempted >= max_items or monotonic() - started >= max_seconds or should_stop():
                break
            attempted += 1
            try:
                doc = self.extract(version_id, depth, should_stop)
            except ExtractionInterrupted as exc:
                # Nothing was recorded for this version, so it is not an
                # attempt: leave the counters honest and end the batch.
                attempted -= 1
                logger.info("Extraction of version %s interrupted: %s", version_id, exc)
                break
            except sqlite3.OperationalError:
                raise
            except ExtractionError:
                raise  # an invalid request for a version we just listed is a bug, not content
            except Exception as exc:  # noqa: BLE001 - per-item isolation
                self._conn.rollback()
                reason = f"unexpected {type(exc).__name__}: {exc}"
                reason = reason.encode("utf-8", "backslashreplace").decode("utf-8")
                doc = self._record_failure(version_id, depth, reason)
            if doc.status == "done":
                done += 1
            else:
                failed += 1
                first_error = first_error or doc.error_reason
                logger.warning("Extraction of version %s failed: %s", version_id, doc.error_reason)

        return ExtractionBatchResult(
            pending=len(version_ids),
            attempted=attempted,
            done=done,
            failed=failed,
            first_error=first_error,
        )

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

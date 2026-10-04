from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from database import ingestion_repository as repo
from ingestion.models import ResourceDescriptor, ResourceVersion
from ingestion.storage import Storage, StorageError
from moodle.client import MoodleClient
from moodle.exceptions import MoodleAuthenticationError, MoodleError

logger = logging.getLogger(__name__)

_URL_MIMETYPE = "text/uri-list"

# Pending-work ordering groups: never seen, changed since the last check,
# previously attempted but never successfully stored (retried last).
_GROUP_NEW, _GROUP_CHANGED, _GROUP_RETRY = 0, 1, 2


@dataclass(frozen=True)
class IngestionBatchResult:
    pending: int
    attempted: int
    succeeded: int
    new_versions: int
    failed: int
    first_error: str | None = None

    @property
    def remaining(self) -> int:
        return self.pending - self.attempted


@dataclass(frozen=True)
class _PendingItem:
    descriptor: ResourceDescriptor
    previous_version_id: int | None
    sort_key: tuple


def _is_systemic(exc: BaseException) -> bool:
    """Failures that will hit every item alike (credentials, the database)
    must fail the job instead of being counted per item."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, (MoodleAuthenticationError, sqlite3.OperationalError)):
            return True
        seen = seen.__cause__
    return False


class IngestionError(Exception):
    """An ingestion attempt failed before producing a valid ResourceVersion.

    No partial/fake ResourceVersion is ever created when this is raised - the
    resource simply remains at whatever version (possibly none) it already
    had, and ingest() can be safely retried later.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ingestion:
    """Preserves original resource content, unchanged and versioned.

    Does not interpret content in any way (no parsing, no extraction, no
    LLM). Its only job: given a ResourceDescriptor, get the bytes, hash them,
    and store a new immutable ResourceVersion only if the content actually
    changed since the last known version.

    "Content" means different things depending on descriptor.source_type:
      - file: the original file's bytes, downloaded via MoodleClient.
      - url:  the canonical URL reference string itself - never the bytes of
        whatever that URL points to. For URL resources, a new version
        represents a change in the preserved reference (e.g. Moodle now
        points this module at a different address), not a change in that
        destination's remote content. Ingestion never fetches, crawls, or
        interprets what a URL points to - see _fetch_content() below.
    """

    def __init__(self, client: MoodleClient, storage: Storage, conn: sqlite3.Connection) -> None:
        self._client = client
        self._storage = storage
        self._conn = conn

    def ingest(self, descriptor: ResourceDescriptor) -> ResourceVersion:
        resource = repo.upsert_resource(self._conn, descriptor)

        content = self._fetch_content(descriptor)
        content_hash = hashlib.sha256(content).hexdigest()

        existing = repo.get_latest_version(self._conn, resource.id)
        if existing is not None and existing.content_hash == content_hash:
            return existing  # unchanged content: idempotent no-op, no new version

        try:
            storage_ref = self._storage.store(content, content_hash)
        except StorageError as exc:
            raise IngestionError(
                f"Storage failed for resource {resource.id} ({descriptor.source_type})"
            ) from exc

        next_version_number = existing.version_number + 1 if existing else 1
        version = ResourceVersion(
            id=None,
            resource_id=resource.id,
            version_number=next_version_number,
            content_hash=content_hash,
            storage_ref=storage_ref,
            size_bytes=len(content),
            mimetype=descriptor.mimetype
            or (_URL_MIMETYPE if descriptor.source_type == "url" else None),
            ingested_at=_now(),
        )
        return repo.insert_resource_version(self._conn, version)

    def pending(self, descriptors: Iterable[ResourceDescriptor]) -> list[ResourceDescriptor]:
        """Descriptors that need an ingest() call, in processing order.

        Read-only and network-free. Pending means:
          - no Resource for this identity yet, or no stored version (an earlier
            attempt failed - retried, but after everything else);
          - url: the preserved reference string differs from the latest version;
          - file: Moodle's timemodified differs from the one confirmed by the
            last successful ingest() (or none was ever confirmed). A changed
            timemodified with identical bytes costs one download and is then
            confirmed - ingest() itself stays the authority on content changes.
        """
        return [item.descriptor for item in self._pending_items(descriptors)]

    def ingest_pending(
        self,
        descriptors: Iterable[ResourceDescriptor],
        max_items: int,
        max_seconds: float,
        should_stop: Callable[[], bool] = lambda: False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> IngestionBatchResult:
        """ingest() pending descriptors within a processing budget.

        Stops (between items) at max_items attempts, max_seconds elapsed, or
        when should_stop() turns true; everything not attempted stays pending
        and is found again next time - work is never dropped by the budget.

        One failing item never stops the batch (DEC-048): its error is
        counted and it stays pending. Only systemic failures (rejected
        credentials, database errors) propagate and fail the whole job.
        """
        items = self._pending_items(descriptors)
        started = monotonic()
        attempted = succeeded = new_versions = failed = 0
        first_error: str | None = None

        for item in items:
            if attempted >= max_items or monotonic() - started >= max_seconds or should_stop():
                break
            attempted += 1
            descriptor = item.descriptor
            try:
                version = self.ingest(descriptor)
                if descriptor.source_type == "file":
                    repo.record_source_check(
                        self._conn, version.resource_id, descriptor.source_timemodified
                    )
            except Exception as exc:  # noqa: BLE001 - per-item isolation, see docstring
                if _is_systemic(exc):
                    raise
                self._conn.rollback()
                failed += 1
                reason = f"{type(exc).__name__}: {exc}"
                if exc.__cause__ is not None:
                    reason += f" (cause: {type(exc.__cause__).__name__}: {exc.__cause__})"
                first_error = first_error or reason
                logger.warning("Ingestion of %s/%s failed: %s",
                               descriptor.origin, descriptor.source_type, reason)
                continue
            succeeded += 1
            if version.id != item.previous_version_id:
                new_versions += 1

        return IngestionBatchResult(
            pending=len(items),
            attempted=attempted,
            succeeded=succeeded,
            new_versions=new_versions,
            failed=failed,
            first_error=first_error,
        )

    def _pending_items(self, descriptors: Iterable[ResourceDescriptor]) -> list[_PendingItem]:
        items: list[_PendingItem] = []
        for d in descriptors:
            state = repo.find_resource_state(self._conn, d.origin, d.source_type, d.external_reference)
            if state is None:
                items.append(_PendingItem(d, None, (_GROUP_NEW, "", d.external_reference)))
                continue
            resource_id, last_attempt_at = state
            latest = repo.get_latest_version(self._conn, resource_id)
            if latest is None:
                items.append(
                    _PendingItem(d, None, (_GROUP_RETRY, last_attempt_at, d.external_reference))
                )
                continue
            if d.source_type == "url":
                changed = hashlib.sha256(d.source_url.encode("utf-8")).hexdigest() != latest.content_hash
            else:
                checked, confirmed_timemodified = repo.get_source_check(self._conn, resource_id)
                changed = not checked or confirmed_timemodified != d.source_timemodified
            if changed:
                items.append(
                    _PendingItem(d, latest.id, (_GROUP_CHANGED, last_attempt_at, d.external_reference))
                )
        items.sort(key=lambda item: item.sort_key)
        return items

    def _fetch_content(self, descriptor: ResourceDescriptor) -> bytes:
        if descriptor.source_type == "file":
            try:
                return self._client.download_file(descriptor.source_url)
            except MoodleError as exc:
                raise IngestionError(
                    f"Download failed for {descriptor.origin}/{descriptor.source_type} "
                    f"resource {descriptor.external_reference!r}"
                ) from exc
        elif descriptor.source_type == "url":
            # Preserve only the canonical reference string. The destination's
            # remote content is never fetched, crawled, or parsed here - so
            # the resulting hash/version tracks changes to the reference
            # itself (e.g. Moodle repointing this module), not to whatever
            # that reference currently leads to.
            return descriptor.source_url.encode("utf-8")
        else:
            raise IngestionError(f"Unsupported source_type: {descriptor.source_type!r}")

"""The four MVP Jobs: a thin boundary between the Scheduler (WHEN) and the
existing services (HOW). A Job never schedules anything and never holds
domain logic - it reads its budget, calls one service method, and reports
counts. Manual and automatic runs use the very same Job objects."""
from __future__ import annotations

import abc
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from database import scheduler_repository as history
from extraction.extract import Extraction
from ingestion.ingest import Ingestion
from ingestion.models import ResourceDescriptor
from ingestion.source_adapters import MoodleFileSourceAdapter, MoodleUrlSourceAdapter
from ingestion.storage import Storage
from moodle.client import MoodleClient
from moodle.sync import MoodleSync
from notifications.service import NotificationService
from scheduler.config import (
    EXTRACTION,
    INGESTION,
    MOODLE_SYNC,
    NOTIFICATIONS,
    BudgetConfig,
)

logger = logging.getLogger(__name__)

TRIGGER_STARTUP = "startup"
TRIGGER_INTERVAL = "interval"
TRIGGER_MANUAL = "manual"
TRIGGER_DEPENDENCY = "dependency"

# Incremental-sync safety margin: `since` = start of the last successful sync
# minus this, so a change stamped by Moodle's clock slightly behind ours, or
# in the same second (Moodle compares strictly `>`), is not skipped.
# Re-detecting a change twice is harmless - every service is idempotent.
CHECKPOINT_OVERLAP_SECONDS = 300

# DEC-058: basic and deep are not differentiated yet; the MVP runs basic only.
EXTRACTION_DEPTH = "basic"


@dataclass(frozen=True)
class RunContext:
    trigger: str
    should_stop: Callable[[], bool]


@dataclass(frozen=True)
class JobResult:
    items_processed: int
    detail: str = ""
    # Per-item problems inside a run that still completed (the job did not
    # fail); kept in execution history so they are never invisible.
    error: str | None = None


class Job(abc.ABC):
    name: str

    def has_work(self) -> bool:
        """Cheap, local, network-free: is there anything for run() to do?
        Used when the job is reached through the dependency chain."""
        return True

    @abc.abstractmethod
    def run(self, context: RunContext) -> JobResult:
        """Do one bounded unit of work. Raising fails this attempt."""


def last_successful_sync_started_at(conn: sqlite3.Connection) -> str | None:
    last = history.last_execution(conn, MOODLE_SYNC, history.STATUS_DONE)
    return last.started_at if last else None


class MoodleSyncJob(Job):
    name = MOODLE_SYNC

    def __init__(
        self, conn: sqlite3.Connection, client_factory: Callable[[], MoodleClient]
    ) -> None:
        self._conn = conn
        self._client_factory = client_factory

    def run(self, context: RunContext) -> JobResult:
        # Startup is a full (reconciling) sync; every other run is incremental
        # from the last successful one. Never-succeeded also means full.
        since = None if context.trigger == TRIGGER_STARTUP else self._checkpoint()
        with self._client_factory() as client:
            report = MoodleSync(client, self._conn).sync_changes(since)
        for warning in report.item_warnings:
            logger.warning("moodle_sync item warning: %s", warning)
        error = None
        if report.item_warnings:
            error = f"{len(report.item_warnings)} item warning(s); first: {report.item_warnings[0]}"
        return JobResult(report.items_processed, report.summary(), error)

    def _checkpoint(self) -> int | None:
        started_at = last_successful_sync_started_at(self._conn)
        if started_at is None:
            return None
        started = int(datetime.fromisoformat(started_at).timestamp())
        return max(0, started - CHECKPOINT_OVERLAP_SECONDS)


class IngestionJob(Job):
    name = INGESTION

    def __init__(
        self,
        conn: sqlite3.Connection,
        storage: Storage,
        client_factory: Callable[[], MoodleClient],
        budget: BudgetConfig,
    ) -> None:
        self._conn = conn
        self._storage = storage
        self._client_factory = client_factory
        self._budget = budget

    def _descriptors(self) -> list[ResourceDescriptor]:
        return [
            *MoodleFileSourceAdapter().discover(self._conn),
            *MoodleUrlSourceAdapter().discover(self._conn),
        ]

    def has_work(self) -> bool:
        with self._client_factory() as client:
            return bool(Ingestion(client, self._storage, self._conn).pending(self._descriptors()))

    def run(self, context: RunContext) -> JobResult:
        with self._client_factory() as client:
            result = Ingestion(client, self._storage, self._conn).ingest_pending(
                self._descriptors(),
                max_items=self._budget.max_items,
                max_seconds=self._budget.max_seconds,
                should_stop=context.should_stop,
            )
        detail = (
            f"{result.attempted}/{result.pending} attempted, {result.new_versions} new version(s), "
            f"{result.failed} failed, {result.remaining} still pending"
        )
        if result.deferred:
            # Kept in execution history so a skipped file is never invisible;
            # `--status` lists them with their sizes.
            detail += f", {result.deferred} deferred (too large)"
        error = f"{result.failed} item(s) failed; first: {result.first_error}" if result.failed else None
        return JobResult(result.attempted, detail, error)


class ExtractionJob(Job):
    name = EXTRACTION

    def __init__(
        self, extraction_factory: Callable[[], Extraction], budget: BudgetConfig
    ) -> None:
        self._extraction_factory = extraction_factory
        self._budget = budget
        self._extraction: Extraction | None = None

    def _get_extraction(self) -> Extraction:
        # Built once so the OCR engine (and its model, loaded lazily on first
        # OCR) is reused across runs instead of reloaded every cycle.
        if self._extraction is None:
            self._extraction = self._extraction_factory()
        return self._extraction

    def has_work(self) -> bool:
        return bool(self._get_extraction().pending_version_ids(EXTRACTION_DEPTH))

    def run(self, context: RunContext) -> JobResult:
        result = self._get_extraction().extract_pending(
            EXTRACTION_DEPTH,
            max_items=self._budget.max_items,
            max_seconds=self._budget.max_seconds,
            should_stop=context.should_stop,
        )
        detail = (
            f"{result.attempted}/{result.pending} attempted, {result.done} done, "
            f"{result.failed} failed, {result.remaining} still pending"
        )
        error = f"{result.failed} item(s) failed; first: {result.first_error}" if result.failed else None
        return JobResult(result.attempted, detail, error)


class NotificationJob(Job):
    name = NOTIFICATIONS

    def __init__(self, conn: sqlite3.Connection, service: NotificationService) -> None:
        self._conn = conn
        self._service = service

    def has_work(self) -> bool:
        return bool(self._service.pending(last_successful_sync_started_at(self._conn)))

    def run(self, context: RunContext) -> JobResult:
        result = self._service.send_pending(last_successful_sync_started_at(self._conn))
        detail = f"{result.sent} sent, {result.failed} failed"
        error = f"{result.failed} not sent; first: {result.first_error}" if result.failed else None
        return JobResult(result.sent, detail, error)

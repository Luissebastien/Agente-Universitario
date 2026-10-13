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
from notifications.models import assignment_id_of
from notifications.service import NotificationService
from scheduler.redaction import safe_error
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

        Asked before every automatic run - through the dependency chain or on
        a clock schedule - so a job that has nothing to do is skipped without
        leaving an execution row. The default says yes: a job that cannot
        answer cheaply should just run.
        """
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
        # Cheap to build: the OCR model is loaded lazily, on the first page
        # that actually needs it, not here.
        if self._extraction is None:
            self._extraction = self._extraction_factory()
        return self._extraction

    def has_work(self) -> bool:
        return bool(self._get_extraction().pending_version_ids(EXTRACTION_DEPTH))

    def run(self, context: RunContext) -> JobResult:
        try:
            result = self._get_extraction().extract_pending(
                EXTRACTION_DEPTH,
                max_items=self._budget.max_items,
                max_seconds=self._budget.max_seconds,
                should_stop=context.should_stop,
            )
        finally:
            # Released after the run, never during it. Measured on the target
            # host: a loaded docTR model holds ~1.1 GB resident, and the
            # scheduler is idle between 6-hourly cycles - so keeping it would
            # pin more than half of a 2 GiB machine's RAM to save the 12.6 s
            # it takes to rebuild from the on-disk cache. The reload only
            # happens in cycles that actually hit OCR, which is rare: ~97% of
            # real PDFs carry a usable text layer (DEC-055).
            self._extraction = None
        detail = (
            f"{result.attempted}/{result.pending} attempted, {result.done} done, "
            f"{result.failed} failed, {result.remaining} still pending"
        )
        error = f"{result.failed} item(s) failed; first: {result.first_error}" if result.failed else None
        return JobResult(result.attempted, detail, error)


class NotificationJob(Job):
    name = NOTIFICATIONS

    def __init__(
        self,
        conn: sqlite3.Connection,
        service: NotificationService,
        client_factory: Callable[[], MoodleClient] | None = None,
    ) -> None:
        self._conn = conn
        self._service = service
        self._client_factory = client_factory

    def has_work(self) -> bool:
        return bool(self._service.pending(last_successful_sync_started_at(self._conn)))

    def run(self, context: RunContext) -> JobResult:
        confirmed_since = last_successful_sync_started_at(self._conn)
        # Reminders run hourly, moodle_sync every six hours, so the stored
        # submission status can be up to a cycle of sync out of date - long
        # enough to remind someone about work they already handed in. Asking
        # Moodle about the few assignments that are about to be notified is
        # what closes that window; most runs have none and call nothing.
        candidates = self._service.pending(confirmed_since)
        self._refresh_submission_status(candidates)
        result = self._service.send_pending(confirmed_since)
        # What the refresh actually prevented. Recorded in the execution
        # detail, not only logged: journald rotates, scheduler_executions is
        # append-only and backed up, and "no reminder arrived" is otherwise
        # indistinguishable from "there was never anything to send".
        suppressed = len(candidates) - result.pending
        detail = f"{result.sent} sent, {result.failed} failed"
        if suppressed > 0:
            detail += f", {suppressed} dropped (submitted since the last sync)"
            logger.info(
                "Dropped %d reminder(s) after re-reading submission status from Moodle: "
                "the work had already been handed in", suppressed,
            )
        error = f"{result.failed} not sent; first: {result.first_error}" if result.failed else None
        return JobResult(result.sent, detail, error)

    def _refresh_submission_status(self, candidates: list) -> None:
        """Re-read from Moodle the submission status of what is about to be sent.

        Best effort by design. If Moodle cannot be reached the run continues
        on the stored status, which is exactly today's behaviour - a reminder
        that is merely redundant is a smaller harm than a deadline that goes
        unmentioned because the site was down.
        """
        if not candidates or self._client_factory is None:
            return
        assignment_ids = sorted(
            {
                assignment_id
                for notification in candidates
                if (assignment_id := assignment_id_of(notification.subject_key)) is not None
            }
        )
        if not assignment_ids:
            return
        try:
            with self._client_factory() as client:
                sync = MoodleSync(client, self._conn)
                for assignment_id in assignment_ids:
                    sync.sync_assignment_submission_status(assignment_id)
            logger.info("Re-read submission status of %d assignment(s) before notifying",
                        len(assignment_ids))
        except Exception as exc:  # noqa: BLE001 - never block a reminder on Moodle
            logger.warning(
                "Could not refresh submission status of %d assignment(s) before notifying; "
                "using the stored status: %s",
                len(assignment_ids), safe_error(exc),
            )

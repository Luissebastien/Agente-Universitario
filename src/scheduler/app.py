"""Process wiring for `python -m scheduler`: builds the Jobs from the existing
services, and runs the daemon, a manual request, or a status report."""
from __future__ import annotations

import logging
import signal
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime, timezone

from database import scheduler_repository as history
from database.db import connect
from extraction.extract import Extraction
from extraction.ocr import DoctrOcrEngine
from ingestion.storage import FilesystemStorage
from moodle.client import MoodleClient
from notifications.providers import LogNotificationProvider, NotificationProvider
from notifications.service import NotificationService
from scheduler.config import (
    EXTRACTION,
    INGESTION,
    MOODLE_SYNC,
    NOTIFICATIONS,
    PIPELINE,
    SchedulerConfig,
)
from scheduler.jobs import (
    TRIGGER_MANUAL,
    ExtractionJob,
    IngestionJob,
    MoodleSyncJob,
    NotificationJob,
)
from scheduler.lock import SingleInstanceLock, is_locked
from scheduler.redaction import SecretRedactingFilter
from scheduler.scheduler import RequestResult, Scheduler

logger = logging.getLogger("scheduler")

EXIT_OK = 0
EXIT_JOB_FAILED = 1
EXIT_UNAVAILABLE = 2
EXIT_INTERRUPTED = 130


def build_scheduler(
    config: SchedulerConfig,
    conn: sqlite3.Connection,
    *,
    client_factory: Callable[[], MoodleClient] = MoodleClient.from_env,
    extraction_factory: Callable[[], Extraction] | None = None,
    provider: NotificationProvider | None = None,
) -> Scheduler:
    """The same Scheduler and Jobs for daemon, manual one-shot and tests.
    The Moodle token is only ever read from the environment, per run, by
    MoodleClient.from_env - never from configuration."""
    storage = FilesystemStorage(config.storage_path)
    if extraction_factory is None:
        def extraction_factory() -> Extraction:
            # DEC-053: docTR is the MVP OCR engine (model loads lazily on first OCR).
            return Extraction(storage, conn, ocr_engine=DoctrOcrEngine())
    service = NotificationService(
        conn, provider or LogNotificationProvider(), config.timezone,
        config.notifications.due_soon_hours,
    )
    jobs = {
        MOODLE_SYNC: MoodleSyncJob(conn, client_factory),
        INGESTION: IngestionJob(conn, storage, client_factory, config.ingestion),
        EXTRACTION: ExtractionJob(extraction_factory, config.extraction),
        NOTIFICATIONS: NotificationJob(conn, service),
    }
    return Scheduler(conn, jobs, config, on_job_failed=service.notify_job_failure)


def configure_logging(config: SchedulerConfig) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_TimezoneFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", config.timezone
    ))
    handler.addFilter(SecretRedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # One line per Moodle HTTP call would drown the job-level log; failures
    # and retries from the client are still WARNING and still shown.
    logging.getLogger("moodle.client").setLevel(logging.WARNING)


def run_daemon(config: SchedulerConfig) -> int:
    lock = SingleInstanceLock(config.lock_path)
    if not lock.try_acquire():
        print(f"Another scheduler process is already running (lock: {config.lock_path}).",
              file=sys.stderr)
        return EXIT_UNAVAILABLE
    conn = _open_database(config)
    previous_handlers = {}
    try:
        scheduler = build_scheduler(config, conn)
        previous_handlers = _install_stop_handlers(scheduler)
        scheduler.startup(one_shot=False)
        scheduler.run_forever()
        return EXIT_OK
    finally:
        _restore_handlers(previous_handlers)
        conn.close()
        lock.release()


def run_manual(config: SchedulerConfig, job_name: str) -> int:
    """Manual execution of one Job (its dependency chain continues normally).

    If no scheduler process is running, this process becomes the scheduler
    (holding the lock) until the work is done. If one is running, the request
    is handed to it: it runs as soon as the scheduler is free, and is dropped
    as a duplicate if that job is already running or queued.
    """
    lock = SingleInstanceLock(config.lock_path)
    if not lock.try_acquire():
        return _hand_over_request(config, job_name)
    conn = _open_database(config)
    previous_handlers = {}
    try:
        scheduler = build_scheduler(config, conn)
        previous_handlers = _install_stop_handlers(scheduler)
        scheduler.startup(one_shot=True)
        result = scheduler.request(job_name, TRIGGER_MANUAL)
        if result is RequestResult.DISABLED:
            print(f"{job_name} is disabled in the configuration "
                  "(see allow_manual_disabled_jobs).", file=sys.stderr)
            return EXIT_UNAVAILABLE
        scheduler.run_until_idle()
        last = history.last_execution(conn, job_name)
        return EXIT_JOB_FAILED if last is not None and last.status == history.STATUS_FAILED else EXIT_OK
    finally:
        _restore_handlers(previous_handlers)
        conn.close()
        lock.release()


def print_status(config: SchedulerConfig) -> int:
    if not config.database_path.exists():
        print("No scheduler database yet - the scheduler has never run with this configuration.")
        return EXIT_OK
    tz = config.timezone
    conn = connect(config.database_path)
    try:
        daemon = "running" if is_locked(config.lock_path) else "not running"
        print(f"Scheduler process: {daemon}")
        running = history.running_executions(conn)
        if running:
            for execution in running:
                print(f"Current job: {execution.job_name} (attempt {execution.attempt}, "
                      f"since {_local(execution.started_at, tz)})")
        else:
            print("Current job: none")
        requests = history.pending_requests(conn)
        if requests:
            print("Manual requests waiting for the scheduler: "
                  + ", ".join(f"{r['job_name']} ({_local(r['requested_at'], tz)})" for r in requests))
        for job_name in PIPELINE:
            print(f"\n[{job_name}] enabled={config.is_enabled(job_name)}")
            last = history.last_execution(conn, job_name)
            if last is None:
                print("  last execution: never")
                continue
            duration = f"{last.duration_seconds:.1f}s" if last.duration_seconds is not None else "-"
            print(f"  last execution: {last.status} at {_local(last.started_at, tz)} "
                  f"(trigger={last.trigger}, attempt {last.attempt}, {duration}, "
                  f"items={last.items_processed if last.items_processed is not None else '-'})")
            if last.detail:
                print(f"  detail: {last.detail}")
            failure = history.last_execution(conn, job_name, history.STATUS_FAILED)
            if failure is not None:
                print(f"  last failure: {_local(failure.started_at, tz)}: {failure.error}")
    finally:
        conn.close()
    return EXIT_OK


# ---- helpers ------------------------------------------------------------

class _TimezoneFormatter(logging.Formatter):
    """Log timestamps in the configured timezone, not the host OS zone."""

    def __init__(self, fmt: str, tz) -> None:
        super().__init__(fmt)
        self._tz = tz

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, self._tz).isoformat(timespec="seconds")


def _open_database(config: SchedulerConfig) -> sqlite3.Connection:
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    return connect(config.database_path)


def _hand_over_request(config: SchedulerConfig, job_name: str) -> int:
    conn = _open_database(config)
    try:
        requested_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        history.add_request(conn, job_name, requested_at)
    except sqlite3.OperationalError as exc:
        print(f"The scheduler database is busy ({exc}); please try again.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    finally:
        conn.close()
    print(f"A scheduler process is running: the {job_name} request was handed to it. It runs as "
          "soon as the scheduler is free (or is dropped if it is already running/queued). "
          "Use --status to follow it.")
    return EXIT_OK


def _install_stop_handlers(scheduler: Scheduler) -> dict:
    """First Ctrl+C / Ctrl+Break / SIGTERM: finish the current item, run
    nothing more, exit cleanly. A second one aborts immediately. On Windows,
    closing the console or killing the process skips this; the next startup
    marks the abandoned attempt as failed."""
    previous: dict = {}

    def handle(signum, frame) -> None:
        logger.warning("Stop requested: finishing the current item, then exiting "
                       "(press Ctrl+C again to abort immediately)")
        scheduler.request_stop()
        _restore_handlers(previous)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, handle)
    return previous


def _restore_handlers(previous: dict) -> None:
    for signum, handler in list(previous.items()):
        signal.signal(signum, handler)
    previous.clear()


def _local(utc_iso: str, tz) -> str:
    return datetime.fromisoformat(utc_iso).astimezone(tz).isoformat(timespec="seconds")

"""Scheduler MVP core: decides WHEN jobs run (.ai/SCHEDULER-MVP-RULES.md).

Single-threaded by construction, so max_concurrent_jobs = 1 always holds
within the process (SingleInstanceLock extends it across processes). This
module deliberately knows nothing about Moodle, documents or academic data:
it only sees Job names, Job results and its own execution history.
"""
from __future__ import annotations

import enum
import logging
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from database import scheduler_repository as history
from scheduler.config import MOODLE_SYNC, PIPELINE, SchedulerConfig
from scheduler.jobs import (
    TRIGGER_DEPENDENCY,
    TRIGGER_INTERVAL,
    TRIGGER_MANUAL,
    TRIGGER_STARTUP,
    Job,
    RunContext,
)
from scheduler.redaction import redact, safe_error

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # spec §9: 3 total attempts per job
MAX_CONCURRENT_JOBS = 1  # spec §10: documentation only - there is one thread
POLL_SECONDS = 5.0  # idle latency for manual requests from other processes
_SLEEP_SLICE_SECONDS = 0.5  # keeps Ctrl+C / stop responsive while waiting


class RequestResult(enum.Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    DISABLED = "disabled"


class Outcome(enum.Enum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"  # reached via the chain but had no work
    FAILED = "failed"  # all attempts failed
    STOPPED = "stopped"  # a stop request interrupted it


@dataclass(frozen=True)
class _QueuedRun:
    job_name: str
    trigger: str


class _AttemptFailed(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


class Scheduler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        jobs: Mapping[str, Job],
        config: SchedulerConfig,
        *,
        on_job_failed: Callable[[str, str, datetime], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        missing = set(PIPELINE) - set(jobs)
        if missing:
            raise ValueError(f"missing job(s): {', '.join(sorted(missing))}")
        self._conn = conn
        self._jobs = dict(jobs)
        self._config = config
        self._on_job_failed = on_job_failed
        self._clock = clock or (lambda: datetime.now(config.timezone))
        self._sleep = sleep
        self._monotonic = monotonic
        self._interval = timedelta(hours=config.moodle_sync.interval_hours)
        self._queue: deque[_QueuedRun] = deque()
        self._running: str | None = None
        self._next_sync_due: datetime | None = None
        self._stop = False

    # ---- public API ----------------------------------------------------

    @property
    def running_job(self) -> str | None:
        return self._running

    @property
    def queued_jobs(self) -> list[str]:
        return [item.job_name for item in self._queue]

    @property
    def next_sync_due(self) -> datetime | None:
        return self._next_sync_due

    @property
    def stop_requested(self) -> bool:
        return self._stop

    def request_stop(self) -> None:
        self._stop = True

    def request(self, job_name: str, trigger: str = TRIGGER_MANUAL) -> RequestResult:
        """Queue a job run unless it is disabled or already running/queued.

        Manual and automatic requests go through here and execute the very
        same Job. A disabled job never runs automatically; a manual request
        for it follows `allow_manual_disabled_jobs`.
        """
        if job_name not in self._jobs:
            raise ValueError(f"unknown job {job_name!r}")
        if not self._config.is_enabled(job_name) and not (
            trigger == TRIGGER_MANUAL and self._config.allow_manual_disabled_jobs
        ):
            logger.info("Not queuing %s (trigger=%s): disabled in configuration", job_name, trigger)
            return RequestResult.DISABLED
        if job_name == self._running or any(item.job_name == job_name for item in self._queue):
            logger.info("Not queuing %s (trigger=%s): already running or queued", job_name, trigger)
            return RequestResult.DUPLICATE
        self._queue.append(_QueuedRun(job_name, trigger))
        logger.info("Queued %s (trigger=%s); queue: %s", job_name, trigger, self.queued_jobs)
        return RequestResult.ACCEPTED

    def startup(self, *, one_shot: bool) -> None:
        """Recover from a previous process, then (daemon mode) queue the
        immediate startup sync and schedule the next one. Missed intervals
        from while the process was not running are never replayed."""
        stale = history.fail_stale_running(
            self._conn, self._now_iso(), "interrupted: the process exited during this attempt"
        )
        if stale:
            logger.warning("Marked %d attempt(s) left 'running' by a previous process as failed", stale)
        for row in history.purge_requests(self._conn):
            logger.warning(
                "Discarded manual request for %s made at %s: it was addressed to a scheduler "
                "process that is gone (requests are never replayed)",
                row["job_name"], row["requested_at"],
            )
        if one_shot:
            return
        if self._config.moodle_sync.enabled:
            self.request(MOODLE_SYNC, TRIGGER_STARTUP)
        else:
            logger.warning("moodle_sync is disabled: no startup sync and no automatic cycles")
        self._next_sync_due = self._clock() + self._interval

    def step(self) -> bool:
        """One loop iteration: pick up manual requests, check the interval,
        run the next queued job. Returns whether a job was taken from the queue."""
        self._handle_manual_requests()
        self._check_interval()
        if not self._queue or self._stop:
            return False
        self._run_next()
        return True

    def run_forever(self) -> None:
        """Daemon loop, until request_stop()."""
        logger.info("Scheduler running; next automatic moodle_sync at %s", self._fmt(self._next_sync_due))
        while not self._stop:
            if not self.step():
                self._idle_wait()
        logger.info("Scheduler stopped; not started: %s", self.queued_jobs or "nothing")

    def run_until_idle(self) -> None:
        """One-shot loop: run the queue (and any manual requests other
        processes hand over meanwhile) until both are empty."""
        while not self._stop and self.step():
            pass

    # ---- loop internals ------------------------------------------------

    def _check_interval(self) -> None:
        if self._next_sync_due is None or not self._config.moodle_sync.enabled:
            return
        now = self._clock()
        if now >= self._next_sync_due:
            self.request(MOODLE_SYNC, TRIGGER_INTERVAL)
            # From *now*, not from the missed due time: after a long sleep or
            # suspend exactly one sync runs, never a burst of catch-up runs.
            self._next_sync_due = now + self._interval

    def _handle_manual_requests(self) -> None:
        for row in history.pending_requests(self._conn):
            job_name, requested_at = row["job_name"], row["requested_at"]
            if job_name not in self._jobs:
                logger.warning("Ignored manual request for unknown job %r", job_name)
            elif history.ran_at_or_after(self._conn, job_name, requested_at):
                # The job was already running when it was requested (or has
                # run since): running it again would be a duplicate.
                logger.info("Manual request for %s made at %s: already running/served",
                            job_name, requested_at)
            else:
                self.request(job_name, TRIGGER_MANUAL)
            history.delete_request(self._conn, row["id"])

    def _idle_wait(self) -> None:
        seconds = POLL_SECONDS
        if self._next_sync_due is not None:
            until_due = (self._next_sync_due - self._clock()).total_seconds()
            seconds = max(0.0, min(seconds, until_due))
        self._sleep_interruptibly(seconds)

    def _sleep_interruptibly(self, seconds: float) -> bool:
        """Wait up to `seconds`; False if a stop was requested meanwhile."""
        deadline = self._monotonic() + seconds
        while not self._stop:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return True
            self._sleep(min(_SLEEP_SLICE_SECONDS, remaining))
        return False

    def _run_next(self) -> None:
        item = self._queue.popleft()
        outcome = self._execute(item)
        if outcome in (Outcome.SUCCEEDED, Outcome.SKIPPED):
            self._continue_chain(item.job_name)
        elif outcome is Outcome.FAILED:
            self._stop_chain(item.job_name)

    def _continue_chain(self, job_name: str) -> None:
        index = PIPELINE.index(job_name)
        if index + 1 >= len(PIPELINE):
            return
        child = PIPELINE[index + 1]
        if not self._config.is_enabled(child):
            # §11: a dependent is eligible only after its parent succeeds, so a
            # disabled job ends the automatic chain instead of being skipped over.
            logger.info("Chain stops after %s: %s is disabled", job_name, child)
            return
        self.request(child, TRIGGER_DEPENDENCY)

    def _stop_chain(self, job_name: str) -> None:
        """A failed job ends its cycle: automatic dependents already queued
        (e.g. from an earlier chain) are cancelled so nothing downstream runs
        as if the upstream state were current. Manual requests are explicit
        user actions and are kept (they log a warning when they run)."""
        downstream = set(PIPELINE[PIPELINE.index(job_name) + 1:])
        kept: deque[_QueuedRun] = deque()
        for item in self._queue:
            if item.job_name in downstream and item.trigger == TRIGGER_DEPENDENCY:
                logger.warning("Cancelled queued %s: upstream %s failed in this cycle",
                               item.job_name, job_name)
            else:
                kept.append(item)
        self._queue = kept
        if downstream:
            logger.warning("Not running %s in this cycle: %s failed",
                           ", ".join(name for name in PIPELINE if name in downstream), job_name)

    def _execute(self, item: _QueuedRun) -> Outcome:
        job = self._jobs[item.job_name]
        self._running = item.job_name
        try:
            last_error = ""
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if self._stop:
                    return Outcome.STOPPED
                if attempt > 1 and not self._sleep_interruptibly(self._config.retry_delay_seconds):
                    return Outcome.STOPPED
                try:
                    ran = self._attempt(job, item, attempt)
                except _AttemptFailed as failure:
                    last_error = failure.error
                    continue
                return Outcome.SUCCEEDED if ran else Outcome.SKIPPED
            self._report_final_failure(item.job_name, last_error)
            return Outcome.FAILED
        finally:
            self._running = None

    def _attempt(self, job: Job, item: _QueuedRun, attempt: int) -> bool:
        """One attempt. True = ran, False = skipped (no work); raises
        _AttemptFailed on failure (already recorded in history)."""
        if item.trigger == TRIGGER_DEPENDENCY:
            try:
                has_work = job.has_work()
            except Exception as exc:  # noqa: BLE001 - unknown state is a failure, never a skip
                self._conn.rollback()
                error = f"could not determine pending work: {safe_error(exc)}"
                now = self._now_iso()
                execution_id = history.start_execution(self._conn, job.name, item.trigger, attempt, now)
                history.finish_execution(self._conn, execution_id, history.STATUS_FAILED, now, 0.0,
                                         error=error)
                logger.error("%s attempt %d/%d failed: %s", job.name, attempt, MAX_ATTEMPTS, error)
                raise _AttemptFailed(error) from None
            if not has_work:
                logger.info("%s: no pending work - skipped", job.name)
                return False
        if attempt == 1 and item.trigger == TRIGGER_MANUAL:
            self._warn_if_upstream_failed(job.name)

        execution_id = history.start_execution(self._conn, job.name, item.trigger, attempt, self._now_iso())
        logger.info("%s attempt %d/%d started (trigger=%s)", job.name, attempt, MAX_ATTEMPTS, item.trigger)
        started = self._monotonic()
        try:
            result = job.run(RunContext(trigger=item.trigger, should_stop=lambda: self._stop))
        except Exception as exc:  # noqa: BLE001 - any job error fails the attempt
            self._conn.rollback()
            error = safe_error(exc)
            history.finish_execution(self._conn, execution_id, history.STATUS_FAILED, self._now_iso(),
                                     self._monotonic() - started, error=error)
            logger.error("%s attempt %d/%d failed after %.1fs: %s",
                         job.name, attempt, MAX_ATTEMPTS, self._monotonic() - started, error)
            raise _AttemptFailed(error) from None
        except BaseException:
            # KeyboardInterrupt (second Ctrl+C) / SystemExit: record, no retry,
            # no failure notification - the process is going away.
            self._conn.rollback()
            history.finish_execution(self._conn, execution_id, history.STATUS_FAILED, self._now_iso(),
                                     self._monotonic() - started,
                                     error="interrupted: attempt aborted by process shutdown")
            raise

        duration = self._monotonic() - started
        detail = redact(result.detail) if result.detail else None
        error = redact(result.error) if result.error else None
        history.finish_execution(self._conn, execution_id, history.STATUS_DONE, self._now_iso(), duration,
                                 items_processed=result.items_processed, detail=detail, error=error)
        logger.info("%s attempt %d/%d done in %.1fs: %d item(s); %s",
                    job.name, attempt, MAX_ATTEMPTS, duration, result.items_processed, detail or "")
        if error:
            logger.warning("%s completed with item errors: %s", job.name, error)
        return True

    def _warn_if_upstream_failed(self, job_name: str) -> None:
        for upstream in PIPELINE[: PIPELINE.index(job_name)]:
            last = history.last_execution(self._conn, upstream)
            if last is not None and last.status == history.STATUS_FAILED:
                logger.warning("Running %s on manual request although upstream %s failed in its "
                               "last attempt: its data may not be current", job_name, upstream)
                return

    def _report_final_failure(self, job_name: str, error: str) -> None:
        logger.error("%s failed after %d attempts: %s", job_name, MAX_ATTEMPTS, error)
        if self._on_job_failed is None:
            return
        try:
            self._on_job_failed(job_name, error, self._clock())
        except Exception as exc:  # noqa: BLE001 - a notification problem never stops the scheduler
            logger.error("Failure notification for %s could not be sent: %s", job_name, safe_error(exc))

    def _now_iso(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat(timespec="microseconds")

    def _fmt(self, moment: datetime | None) -> str:
        if moment is None:
            return "never (one-shot)"
        return moment.astimezone(self._config.timezone).isoformat(timespec="seconds")

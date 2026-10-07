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
from scheduler.config import ALL_JOBS, MOODLE_SYNC, PIPELINE, SchedulerConfig
from scheduler.schedules import next_aligned
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
        missing = set(ALL_JOBS) - set(jobs)
        if missing:
            raise ValueError(f"missing job(s): {', '.join(sorted(missing))}")
        self._conn = conn
        self._jobs = dict(jobs)
        self._config = config
        self._on_job_failed = on_job_failed
        self._clock = clock or (lambda: datetime.now(config.timezone))
        self._sleep = sleep
        self._monotonic = monotonic
        self._periods = config.schedules
        self._queue: deque[_QueuedRun] = deque()
        self._running: str | None = None
        # Job -> the clock instant its next automatic run is due. Held only in
        # memory: the grid is a pure function of the clock (see schedules.py),
        # so a restart recomputes exactly the same instants.
        self._next_run: dict[str, datetime] = {}
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
        return self._next_run.get(MOODLE_SYNC)

    @property
    def next_run_times(self) -> dict[str, datetime]:
        """When each scheduled job is next due, for status output and tests."""
        return dict(self._next_run)

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
        # Every scheduled job joins its clock grid at the next future slot.
        # Only moodle_sync gets a run at startup, deliberately: it reconciles
        # state the process may have missed, while an off-grid notification
        # run would just be a reminder at an arbitrary minute.
        now = self._clock()
        for job_name, period in self._periods.items():
            if not self._config.is_enabled(job_name):
                continue
            self._next_run[job_name] = next_aligned(now, period)
            logger.info("%s scheduled every %dh; next run at %s",
                        job_name, period, self._fmt(self._next_run[job_name]))

    def step(self) -> bool:
        """One loop iteration: pick up manual requests, check the interval,
        run the next queued job. Returns whether a job was taken from the queue."""
        self._handle_manual_requests()
        self._check_schedules()
        if not self._queue or self._stop:
            return False
        self._run_next()
        return True

    def run_forever(self) -> None:
        """Daemon loop, until request_stop()."""
        logger.info("Scheduler running; next automatic runs: %s", self._fmt_schedules())
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

    def _check_schedules(self) -> None:
        """Queue every scheduled job whose slot has arrived.

        Walked in ALL_JOBS order so that when two grids coincide - 00:00 is on
        both an hourly and a six-hourly one - the order is always the same and
        the sync goes first. They still run one at a time: this only queues.
        """
        now = self._clock()
        for job_name in ALL_JOBS:
            due = self._next_run.get(job_name)
            if due is None or now < due:
                continue
            self.request(job_name, TRIGGER_INTERVAL)
            # Computed from *now*, so slots missed while the process was down
            # or asleep are skipped rather than replayed as a burst.
            self._next_run[job_name] = next_aligned(now, self._periods[job_name])

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
        """Sleep until the earliest of: the next poll, the next scheduled slot."""
        now = self._clock()
        seconds = POLL_SECONDS
        for due in self._next_run.values():
            seconds = min(seconds, max(0.0, (due - now).total_seconds()))
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

    def _downstream_of(self, job_name: str) -> tuple[str, ...]:
        """The chain jobs that follow this one - empty for a job that is not
        part of the chain at all (notifications), which therefore neither
        triggers anything downstream nor cancels anything when it fails."""
        if job_name not in PIPELINE:
            return ()
        return PIPELINE[PIPELINE.index(job_name) + 1:]

    def _continue_chain(self, job_name: str) -> None:
        downstream = self._downstream_of(job_name)
        if not downstream:
            return
        child = downstream[0]
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
        downstream = set(self._downstream_of(job_name))
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
        # Asked for every automatic trigger, not just chained ones: a job on a
        # clock schedule wakes up far more often than it has work, and a run
        # that found nothing to do should leave no execution row behind. A
        # manual request always runs - the user asked for it explicitly.
        if item.trigger in (TRIGGER_DEPENDENCY, TRIGGER_INTERVAL):
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
        if job_name not in PIPELINE:
            return  # not part of the chain, so it has no upstream to be stale
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

    def _fmt_schedules(self) -> str:
        return ", ".join(
            f"{name} at {self._fmt(self._next_run[name])}"
            for name in ALL_JOBS
            if name in self._next_run
        ) or "none scheduled"

    def _fmt(self, moment: datetime | None) -> str:
        if moment is None:
            return "never (one-shot)"
        return moment.astimezone(self._config.timezone).isoformat(timespec="seconds")

import dataclasses
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from database import scheduler_repository as history
from database.db import connect
from scheduler.config import ALL_JOBS, EXTRACTION, INGESTION, MOODLE_SYNC, NOTIFICATIONS, PIPELINE
from scheduler.jobs import (
    TRIGGER_DEPENDENCY,
    TRIGGER_INTERVAL,
    TRIGGER_MANUAL,
    TRIGGER_STARTUP,
    JobResult,
)
from scheduler.scheduler import MAX_ATTEMPTS, RequestResult, Scheduler

from .scheduler_fakes import TZ, FakeJob, FakeTime, disabled, make_config

START = datetime(2026, 10, 2, 8, 0, tzinfo=TZ)
HOUR = 3600


class SchedulerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.time = FakeTime(START)
        self.jobs = {name: FakeJob(name, time=self.time) for name in ALL_JOBS}
        self.failures: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        self.conn.close()

    def make_scheduler(self, config=None) -> Scheduler:
        return Scheduler(
            self.conn,
            self.jobs,
            config or make_config(retry_delay_seconds=0),
            on_job_failed=lambda name, error, at: self.failures.append((name, error)),
            clock=self.time.clock,
            sleep=self.time.sleep,
            monotonic=self.time.monotonic,
        )

    def drain(self, scheduler: Scheduler) -> None:
        while scheduler.step():
            pass

    def runs(self, name: str) -> list[str]:
        return [ctx.trigger for ctx in self.jobs[name].runs]

    def rows(self, name: str | None = None):
        sql = "SELECT * FROM scheduler_executions"
        params: tuple = ()
        if name:
            sql += " WHERE job_name = ?"
            params = (name,)
        return self.conn.execute(sql + " ORDER BY id", params).fetchall()


class SchedulingTests(SchedulerTestCase):
    def test_startup_runs_a_fresh_moodle_sync_immediately(self) -> None:
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)

        self.assertEqual(scheduler.queued_jobs, [MOODLE_SYNC])
        self.drain(scheduler)
        self.assertEqual(self.runs(MOODLE_SYNC), [TRIGGER_STARTUP])

    def test_runs_land_on_clock_times_not_on_the_startup_time(self) -> None:
        # START is 08:00 and the sync grid is 00/06/12/18, so the next run is
        # at 12:00 - four hours away - not six hours after this process began.
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.assertEqual(scheduler.next_sync_due, START.replace(hour=12))

    def test_each_scheduled_job_keeps_its_own_grid(self) -> None:
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)

        self.assertEqual(
            scheduler.next_run_times,
            {MOODLE_SYNC: START.replace(hour=12), NOTIFICATIONS: START.replace(hour=9)},
        )

    def test_job_not_due_before_its_slot(self) -> None:
        # Only the sync grid, so nothing else can make step() do work here.
        scheduler = self.make_scheduler(disabled(make_config(retry_delay_seconds=0), NOTIFICATIONS))
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        self.time.advance(4 * HOUR - 61)  # 11:58:59, just short of the 12:00 slot
        self.assertFalse(scheduler.step())
        self.assertEqual(self.runs(MOODLE_SYNC), [TRIGGER_STARTUP])

    def test_job_due_after_the_interval(self) -> None:
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        self.time.advance(4 * HOUR)  # 12:00
        self.drain(scheduler)
        self.assertEqual(self.runs(MOODLE_SYNC), [TRIGGER_STARTUP, TRIGGER_INTERVAL])

    def test_missed_intervals_are_not_replayed(self) -> None:
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        self.time.advance(24 * HOUR)  # e.g. laptop suspended for a day: 4 slots missed
        self.drain(scheduler)

        # Exactly one run, and the grid is rejoined at the next future slot
        # (08:00 the next day -> 12:00), never caught up slot by slot.
        self.assertEqual(self.runs(MOODLE_SYNC), [TRIGGER_STARTUP, TRIGGER_INTERVAL])
        self.assertEqual(scheduler.next_sync_due, self.time.now.replace(hour=12))

    def test_notifications_run_on_their_own_grid_not_through_the_chain(self) -> None:
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)  # startup sync and the whole chain

        self.assertEqual(self.jobs[NOTIFICATIONS].runs, [])  # not a chain member

        self.time.advance(HOUR)  # 09:00, its own hourly slot
        self.drain(scheduler)
        self.assertEqual(self.runs(NOTIFICATIONS), [TRIGGER_INTERVAL])

    def test_notifications_still_run_when_the_sync_chain_failed(self) -> None:
        # The whole point of taking it off the chain: reminders are decided
        # from state already stored locally, so a Moodle outage must not
        # silence them. (confirmed_since, inside the service, is what keeps
        # unconfirmed data out - not the scheduling.)
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("moodle down")] * MAX_ATTEMPTS
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)
        self.time.advance(HOUR)
        self.drain(scheduler)

        self.assertEqual(self.runs(NOTIFICATIONS), [TRIGGER_INTERVAL])

    def test_two_grids_falling_together_both_run_and_sync_goes_first(self) -> None:
        order: list[str] = []
        for job in self.jobs.values():
            job.during_run = lambda j: order.append(j.name)
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)
        order.clear()

        self.time.advance(4 * HOUR)  # 12:00 is on both the 6h and the 1h grid
        self.drain(scheduler)

        self.assertEqual(self.runs(MOODLE_SYNC)[-1], TRIGGER_INTERVAL)
        self.assertEqual(self.runs(NOTIFICATIONS), [TRIGGER_INTERVAL])
        self.assertEqual(order[0], MOODLE_SYNC)  # the coinciding grids run sync first
        self.assertIn(NOTIFICATIONS, order)
        self.assertEqual(len(order), len(set(order)))  # each ran once, one at a time

    def test_a_scheduled_job_with_nothing_to_do_leaves_no_execution_row(self) -> None:
        # An hourly job is idle most of the time; those runs must not fill the
        # append-only history with rows saying nothing happened.
        self.jobs[NOTIFICATIONS]._has_work = False
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)
        self.time.advance(HOUR)
        self.drain(scheduler)

        self.assertEqual(self.jobs[NOTIFICATIONS].runs, [])
        self.assertEqual(self.rows(NOTIFICATIONS), [])
        self.assertEqual(self.jobs[NOTIFICATIONS].has_work_calls, 1)

    def test_a_manual_run_happens_even_with_no_work_and_does_not_move_the_grid(self) -> None:
        self.jobs[NOTIFICATIONS]._has_work = False
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        self.time.advance(30 * 60)  # 08:30
        scheduler.request(NOTIFICATIONS, TRIGGER_MANUAL)
        self.drain(scheduler)

        self.assertEqual(self.runs(NOTIFICATIONS), [TRIGGER_MANUAL])
        self.assertEqual(scheduler.next_run_times[NOTIFICATIONS], START.replace(hour=9))

    def test_interval_is_configurable(self) -> None:
        config = make_config(
            retry_delay_seconds=0,
            moodle_sync=dataclasses.replace(make_config().moodle_sync, interval_hours=1),
        )
        scheduler = self.make_scheduler(config)
        scheduler.startup(one_shot=False)
        self.drain(scheduler)
        self.time.advance(HOUR)
        self.drain(scheduler)
        self.assertEqual(len(self.runs(MOODLE_SYNC)), 2)

    def test_disabled_job_never_runs_automatically(self) -> None:
        scheduler = self.make_scheduler(disabled(make_config(retry_delay_seconds=0), MOODLE_SYNC))
        scheduler.startup(one_shot=False)
        self.drain(scheduler)
        self.time.advance(7 * HOUR)
        self.drain(scheduler)
        self.assertEqual(self.runs(MOODLE_SYNC), [])

    def test_manual_request_for_disabled_job_is_refused_by_default(self) -> None:
        scheduler = self.make_scheduler(disabled(make_config(retry_delay_seconds=0), EXTRACTION))
        self.assertIs(scheduler.request(EXTRACTION, TRIGGER_MANUAL), RequestResult.DISABLED)
        self.drain(scheduler)
        self.assertEqual(self.runs(EXTRACTION), [])

    def test_manual_request_for_disabled_job_allowed_when_configured(self) -> None:
        config = disabled(make_config(retry_delay_seconds=0, allow_manual_disabled_jobs=True), EXTRACTION)
        scheduler = self.make_scheduler(config)
        self.assertIs(scheduler.request(EXTRACTION, TRIGGER_MANUAL), RequestResult.ACCEPTED)
        self.drain(scheduler)
        self.assertEqual(self.runs(EXTRACTION), [TRIGGER_MANUAL])

    def test_manual_execution_runs_the_same_job_object_as_automatic(self) -> None:
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)
        scheduler.request(MOODLE_SYNC, TRIGGER_MANUAL)
        self.drain(scheduler)
        self.assertEqual(self.runs(MOODLE_SYNC), [TRIGGER_STARTUP, TRIGGER_MANUAL])

    def test_unknown_job_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.make_scheduler().request("forums")


class DependencyTests(SchedulerTestCase):
    def test_successful_parent_enables_the_whole_chain_in_order(self) -> None:
        order: list[str] = []
        for job in self.jobs.values():
            job.during_run = lambda j: order.append(j.name)
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        self.assertEqual(order, list(PIPELINE))
        self.assertEqual(self.runs(INGESTION), [TRIGGER_DEPENDENCY])

    def test_no_change_sync_does_not_trigger_downstream_work(self) -> None:
        for name in (INGESTION, EXTRACTION):
            self.jobs[name]._has_work = False
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        for name in (INGESTION, EXTRACTION):
            self.assertEqual(self.jobs[name].runs, [], name)
            self.assertEqual(self.rows(name), [], name)  # a skip is not an execution
        self.assertEqual(len(self.rows(MOODLE_SYNC)), 1)

    def test_leftover_work_downstream_still_runs_after_an_idle_stage(self) -> None:
        self.jobs[INGESTION]._has_work = False  # nothing new to ingest...
        scheduler = self.make_scheduler()  # ...but extraction still has budget-limited leftovers
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        self.assertEqual(self.jobs[INGESTION].runs, [])
        self.assertEqual(self.runs(EXTRACTION), [TRIGGER_DEPENDENCY])

    def test_failed_parent_blocks_downstream(self) -> None:
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("moodle down")] * MAX_ATTEMPTS
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        for name in (INGESTION, EXTRACTION):
            self.assertEqual(self.jobs[name].runs, [], name)
            self.assertEqual(self.jobs[name].has_work_calls, 0, name)

    def test_failed_middle_job_blocks_only_its_dependents(self) -> None:
        self.jobs[INGESTION].outcomes = [RuntimeError("disk full")] * MAX_ATTEMPTS
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        self.assertEqual(len(self.runs(MOODLE_SYNC)), 1)
        self.assertEqual(len(self.runs(INGESTION)), MAX_ATTEMPTS)
        self.assertEqual(self.jobs[EXTRACTION].runs, [])

    def test_already_queued_automatic_dependent_is_cancelled_when_upstream_fails(self) -> None:
        # Regression for the design review: a dependent queued by an earlier
        # chain must not run after a later upstream failure in its cycle.
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("moodle down")] * MAX_ATTEMPTS
        scheduler = self.make_scheduler()
        scheduler.request(MOODLE_SYNC, TRIGGER_INTERVAL)
        scheduler.request(EXTRACTION, TRIGGER_DEPENDENCY)
        self.drain(scheduler)

        self.assertEqual(self.jobs[EXTRACTION].runs, [])

    def test_manual_request_queued_behind_a_failing_upstream_is_still_honored(self) -> None:
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("moodle down")] * MAX_ATTEMPTS
        scheduler = self.make_scheduler()
        scheduler.request(MOODLE_SYNC, TRIGGER_INTERVAL)
        scheduler.request(EXTRACTION, TRIGGER_MANUAL)
        with self.assertLogs("scheduler.scheduler", level="WARNING") as logs:
            self.drain(scheduler)

        self.assertEqual(self.runs(EXTRACTION), [TRIGGER_MANUAL])
        self.assertTrue(any("manual request" in line for line in logs.output))

    def test_disabled_job_ends_the_automatic_chain(self) -> None:
        scheduler = self.make_scheduler(disabled(make_config(retry_delay_seconds=0), INGESTION))
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        self.assertEqual(len(self.runs(MOODLE_SYNC)), 1)
        self.assertEqual(self.jobs[INGESTION].runs, [])
        self.assertEqual(self.jobs[EXTRACTION].runs, [])
        self.assertEqual(self.jobs[NOTIFICATIONS].runs, [])

    def test_has_work_error_is_a_failure_never_a_skip(self) -> None:
        self.jobs[INGESTION]._has_work = RuntimeError("database is corrupt")
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        rows = self.rows(INGESTION)
        self.assertEqual([r["status"] for r in rows], ["failed"] * MAX_ATTEMPTS)
        self.assertIn("could not determine pending work", rows[0]["error"])
        self.assertEqual(self.jobs[EXTRACTION].runs, [])
        self.assertEqual([name for name, _ in self.failures], [INGESTION])


class RetryTests(SchedulerTestCase):
    def test_first_failure_is_retried(self) -> None:
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("timeout")]
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        rows = self.rows(MOODLE_SYNC)
        self.assertEqual([(r["attempt"], r["status"]) for r in rows], [(1, "failed"), (2, "done")])
        self.assertEqual(len(self.runs(INGESTION)), 1)  # chain continues after the retry succeeds
        self.assertEqual(self.failures, [])

    def test_second_failure_is_retried(self) -> None:
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("timeout"), RuntimeError("timeout")]
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        rows = self.rows(MOODLE_SYNC)
        self.assertEqual([(r["attempt"], r["status"]) for r in rows],
                         [(1, "failed"), (2, "failed"), (3, "done")])

    def test_third_failure_marks_the_job_failed_and_notifies_once(self) -> None:
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("boom")] * MAX_ATTEMPTS
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        rows = self.rows(MOODLE_SYNC)
        self.assertEqual([(r["attempt"], r["status"]) for r in rows],
                         [(1, "failed"), (2, "failed"), (3, "failed")])
        self.assertEqual(len(self.runs(MOODLE_SYNC)), MAX_ATTEMPTS)  # never a 4th attempt
        self.assertEqual(self.failures, [(MOODLE_SYNC, "RuntimeError: boom")])

    def test_next_automatic_cycle_retries_normally(self) -> None:
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("boom")] * MAX_ATTEMPTS
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        self.time.advance(6 * HOUR)
        self.drain(scheduler)

        last = history.last_execution(self.conn, MOODLE_SYNC)
        self.assertEqual((last.trigger, last.attempt, last.status), (TRIGGER_INTERVAL, 1, "done"))
        self.assertEqual(len(self.runs(INGESTION)), 1)

    def test_waits_the_configured_delay_between_attempts(self) -> None:
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("timeout")]
        scheduler = self.make_scheduler(make_config(retry_delay_seconds=60))
        scheduler.request(MOODLE_SYNC, TRIGGER_MANUAL)
        self.drain(scheduler)

        self.assertAlmostEqual(sum(self.time.sleeps), 60)
        rows = self.rows(MOODLE_SYNC)
        gap = datetime.fromisoformat(rows[1]["started_at"]) - datetime.fromisoformat(rows[0]["started_at"])
        self.assertGreaterEqual(gap, timedelta(seconds=60))

    def test_stop_during_retry_wait_abandons_remaining_attempts_without_notifying(self) -> None:
        self.jobs[MOODLE_SYNC].outcomes = [RuntimeError("timeout")] * MAX_ATTEMPTS
        scheduler = self.make_scheduler(make_config(retry_delay_seconds=60))
        self.time.on_sleep = lambda seconds: scheduler.request_stop()
        scheduler.request(MOODLE_SYNC, TRIGGER_MANUAL)
        self.drain(scheduler)

        self.assertEqual(len(self.runs(MOODLE_SYNC)), 1)
        self.assertEqual(self.failures, [])
        self.assertEqual(self.jobs[INGESTION].runs, [])


class ConcurrencyTests(SchedulerTestCase):
    def test_only_one_job_runs_at_a_time(self) -> None:
        counter = {"now": 0, "max": 0}
        for job in self.jobs.values():
            job.active_counter = counter
        scheduler = self.make_scheduler()
        self.jobs[MOODLE_SYNC].during_run = lambda job: scheduler.request(EXTRACTION, TRIGGER_MANUAL)
        scheduler.startup(one_shot=False)
        self.drain(scheduler)

        self.assertEqual(counter["max"], 1)
        self.assertEqual(scheduler.running_job, None)

    def test_duplicate_queued_request_is_not_enqueued(self) -> None:
        scheduler = self.make_scheduler()
        self.assertIs(scheduler.request(MOODLE_SYNC), RequestResult.ACCEPTED)
        self.assertIs(scheduler.request(MOODLE_SYNC), RequestResult.DUPLICATE)
        self.assertEqual(scheduler.queued_jobs, [MOODLE_SYNC])

    def test_request_for_the_running_job_is_a_duplicate(self) -> None:
        results = []
        scheduler = self.make_scheduler()
        self.jobs[MOODLE_SYNC].during_run = lambda job: results.append(scheduler.request(MOODLE_SYNC))
        scheduler.request(MOODLE_SYNC)
        self.drain(scheduler)

        self.assertEqual(results, [RequestResult.DUPLICATE])
        self.assertEqual(len(self.runs(MOODLE_SYNC)), 1)

    def test_other_job_requested_while_busy_is_queued_and_runs_afterwards(self) -> None:
        order: list[str] = []
        scheduler = self.make_scheduler()
        def during_extraction(job):
            order.append(EXTRACTION)
            if len(job.runs) == 1:  # request once; the chain brings extraction back later
                scheduler.request(MOODLE_SYNC, TRIGGER_MANUAL)

        self.jobs[EXTRACTION].during_run = during_extraction
        self.jobs[MOODLE_SYNC].during_run = lambda job: order.append(MOODLE_SYNC)
        scheduler.request(EXTRACTION, TRIGGER_MANUAL)
        self.drain(scheduler)

        self.assertEqual(order[:2], [EXTRACTION, MOODLE_SYNC])


class StartupRecoveryTests(SchedulerTestCase):
    def test_attempts_left_running_by_a_dead_process_are_marked_failed(self) -> None:
        history.start_execution(self.conn, INGESTION, TRIGGER_DEPENDENCY, 1, "2026-10-01T10:00:00.000000+00:00")
        self.make_scheduler().startup(one_shot=True)

        row = self.rows(INGESTION)[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("interrupted", row["error"])

    def test_leftover_manual_requests_are_discarded_not_replayed(self) -> None:
        history.add_request(self.conn, EXTRACTION, "2026-09-28T10:00:00.000000+00:00")
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=True)
        self.drain(scheduler)

        self.assertEqual(self.jobs[EXTRACTION].runs, [])
        self.assertEqual(history.pending_requests(self.conn), [])


class ManualRequestInboxTests(SchedulerTestCase):
    def _now_iso(self) -> str:
        return self.time.now.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def test_request_from_another_process_is_picked_up_and_run(self) -> None:
        scheduler = self.make_scheduler()
        history.add_request(self.conn, EXTRACTION, self._now_iso())
        self.drain(scheduler)

        self.assertEqual(self.runs(EXTRACTION), [TRIGGER_MANUAL])
        self.assertEqual(history.pending_requests(self.conn), [])

    def test_request_made_while_that_job_was_running_is_a_duplicate(self) -> None:
        scheduler = self.make_scheduler()
        # Extraction runs; meanwhile another process asks for extraction.
        self.jobs[EXTRACTION].duration = 30
        self.jobs[EXTRACTION].during_run = lambda job: history.add_request(
            self.conn, EXTRACTION, self._now_iso())
        scheduler.request(EXTRACTION, TRIGGER_MANUAL)
        self.drain(scheduler)

        self.assertEqual(len(self.runs(EXTRACTION)), 1)
        self.assertEqual(history.pending_requests(self.conn), [])


class PersistenceTests(SchedulerTestCase):
    def test_history_stores_job_times_status_attempt_duration_and_items(self) -> None:
        self.jobs[MOODLE_SYNC].outcomes = [JobResult(7, "incremental: 2/8 course(s) re-synced")]
        self.jobs[MOODLE_SYNC].duration = 12.5
        scheduler = self.make_scheduler()
        scheduler.request(MOODLE_SYNC, TRIGGER_MANUAL)
        self.drain(scheduler)

        row = self.rows(MOODLE_SYNC)[0]
        self.assertEqual((row["job_name"], row["trigger"], row["attempt"], row["status"]),
                         (MOODLE_SYNC, TRIGGER_MANUAL, 1, "done"))
        self.assertEqual(row["items_processed"], 7)
        self.assertEqual(row["detail"], "incremental: 2/8 course(s) re-synced")
        self.assertAlmostEqual(row["duration_seconds"], 12.5)
        started = datetime.fromisoformat(row["started_at"])
        finished = datetime.fromisoformat(row["finished_at"])
        self.assertEqual(started.utcoffset(), timedelta(0))  # stored in UTC, like every other table
        self.assertEqual(started, START)
        self.assertEqual(finished - started, timedelta(seconds=12.5))
        self.assertIsNone(row["error"])

    def test_errors_and_attempt_numbers_are_retained(self) -> None:
        self.jobs[MOODLE_SYNC].outcomes = [ValueError("first"), ConnectionError("second")]
        scheduler = self.make_scheduler()
        scheduler.request(MOODLE_SYNC, TRIGGER_MANUAL)
        self.drain(scheduler)

        rows = self.rows(MOODLE_SYNC)
        self.assertEqual([(r["attempt"], r["error"]) for r in rows],
                         [(1, "ValueError: first"), (2, "ConnectionError: second"), (3, None)])

    def test_secrets_never_reach_execution_history_or_failure_notifications(self) -> None:
        fake_token = "0123456789abcdef0123456789abcdef"
        leak = RuntimeError(f"GET https://x/pluginfile.php/1/a.pdf?token={fake_token} failed ({fake_token})")
        self.jobs[MOODLE_SYNC].outcomes = [leak] * MAX_ATTEMPTS
        scheduler = self.make_scheduler()
        scheduler.request(MOODLE_SYNC, TRIGGER_MANUAL)
        with patch.dict(os.environ, {"MOODLE_TOKEN": fake_token}):
            self.drain(scheduler)

        for row in self.rows(MOODLE_SYNC):
            self.assertNotIn(fake_token, row["error"])
            self.assertIn("token=***", row["error"])
        self.assertNotIn(fake_token, self.failures[0][1])

    def test_item_errors_of_a_completed_run_are_kept_visible(self) -> None:
        self.jobs[INGESTION].outcomes = [JobResult(3, "3/3 attempted", "1 item(s) failed; first: 404")]
        scheduler = self.make_scheduler()
        scheduler.request(INGESTION, TRIGGER_MANUAL)
        self.drain(scheduler)

        row = self.rows(INGESTION)[0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["error"], "1 item(s) failed; first: 404")

    def test_keyboard_interrupt_is_recorded_and_not_retried(self) -> None:
        self.jobs[EXTRACTION].outcomes = [KeyboardInterrupt()]
        scheduler = self.make_scheduler()
        scheduler.request(EXTRACTION, TRIGGER_MANUAL)
        with self.assertRaises(KeyboardInterrupt):
            self.drain(scheduler)

        rows = self.rows(EXTRACTION)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("interrupted", rows[0]["error"])
        self.assertEqual(self.failures, [])


class TimezoneTests(SchedulerTestCase):
    def test_default_clock_uses_the_configured_timezone_not_the_host(self) -> None:
        scheduler = Scheduler(self.conn, self.jobs, make_config(retry_delay_seconds=0))
        scheduler.startup(one_shot=False)

        self.assertEqual(scheduler.next_sync_due.tzinfo, TZ)
        self.assertEqual(scheduler.next_sync_due.utcoffset(), timedelta(hours=-4))


class LoopTests(SchedulerTestCase):
    def test_run_forever_returns_after_a_stop_request(self) -> None:
        scheduler = self.make_scheduler()
        scheduler.startup(one_shot=False)
        self.time.on_sleep = lambda seconds: scheduler.request_stop()
        scheduler.run_forever()

        self.assertEqual(len(self.runs(MOODLE_SYNC)), 1)
        self.assertTrue(scheduler.stop_requested)

    def test_stop_lets_the_current_job_finish_and_starts_nothing_else(self) -> None:
        scheduler = self.make_scheduler()
        self.jobs[MOODLE_SYNC].during_run = lambda job: scheduler.request_stop()
        scheduler.startup(one_shot=False)
        scheduler.run_forever()

        self.assertEqual(self.rows(MOODLE_SYNC)[0]["status"], "done")
        self.assertEqual(self.jobs[INGESTION].runs, [])

    def test_should_stop_is_visible_to_the_running_job(self) -> None:
        seen = []
        scheduler = self.make_scheduler()

        def during(job):
            seen.append(job.runs[-1].should_stop())
            scheduler.request_stop()
            seen.append(job.runs[-1].should_stop())

        self.jobs[EXTRACTION].during_run = during
        scheduler.request(EXTRACTION, TRIGGER_MANUAL)
        self.drain(scheduler)
        self.assertEqual(seen, [False, True])


class BoundaryTests(unittest.TestCase):
    def test_scheduler_core_has_no_academic_or_notification_logic(self) -> None:
        source = (Path(__file__).resolve().parents[2] / "src" / "scheduler" / "scheduler.py").read_text(
            encoding="utf-8")
        imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
        for forbidden in ("read_model", "notifications", "moodle.", "ingestion", "extraction"):
            self.assertFalse(any(forbidden in line for line in imports), forbidden)
        self.assertNotIn("core_course_get_updates_since", source)

    def test_missing_job_is_a_construction_error(self) -> None:
        conn = connect(":memory:")
        try:
            with self.assertRaises(ValueError):
                Scheduler(conn, {MOODLE_SYNC: FakeJob(MOODLE_SYNC)}, make_config())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

"""Shared fakes for Scheduler unit tests: a controllable clock and Jobs that
record how they were called. No Moodle, no network, no real sleeping."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scheduler.config import (
    BudgetConfig,
    MoodleSyncConfig,
    NotificationsConfig,
    SchedulerConfig,
)
from scheduler.jobs import Job, JobResult, RunContext

TZ = ZoneInfo("America/Santo_Domingo")


def make_config(**overrides) -> SchedulerConfig:
    config = SchedulerConfig(
        timezone=TZ,
        database_path=Path("unused.sqlite3"),
        storage_path=Path("unused-originals"),
        retry_delay_seconds=60,
        allow_manual_disabled_jobs=False,
        moodle_sync=MoodleSyncConfig(enabled=True, interval_hours=6),
        ingestion=BudgetConfig(enabled=True, max_items=100, max_seconds=1800),
        extraction=BudgetConfig(enabled=True, max_items=50, max_seconds=1800),
        notifications=NotificationsConfig(enabled=True, due_soon_hours=24),
    )
    return dataclasses.replace(config, **overrides)


def disabled(config: SchedulerConfig, job_name: str) -> SchedulerConfig:
    section = getattr(config, job_name)
    return dataclasses.replace(config, **{job_name: dataclasses.replace(section, enabled=False)})


class FakeTime:
    """Wall clock (aware, America/Santo_Domingo) and monotonic clock that
    only move when a test (or a fake sleep) moves them."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self._mono = 0.0
        self.sleeps: list[float] = []
        self.on_sleep = None  # optional hook(seconds)

    def clock(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self._mono += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)


class FakeJob(Job):
    """`outcomes` is consumed one per run: an exception instance is raised,
    a JobResult is returned; when exhausted, runs succeed with 1 item."""

    def __init__(self, name: str, has_work=True, outcomes=None, duration: float = 0.0,
                 time: FakeTime | None = None, during_run=None, active_counter=None) -> None:
        self.name = name
        self._has_work = has_work
        self.outcomes = list(outcomes or [])
        self.duration = duration
        self.time = time
        self.during_run = during_run
        self.active_counter = active_counter
        self.runs: list[RunContext] = []
        self.has_work_calls = 0

    def has_work(self) -> bool:
        self.has_work_calls += 1
        if isinstance(self._has_work, BaseException):
            raise self._has_work
        return self._has_work

    def run(self, context: RunContext) -> JobResult:
        self.runs.append(context)
        if self.active_counter is not None:
            self.active_counter["now"] += 1
            self.active_counter["max"] = max(self.active_counter["max"], self.active_counter["now"])
        try:
            if self.during_run is not None:
                self.during_run(self)
            if self.time is not None and self.duration:
                self.time.advance(self.duration)
            outcome = self.outcomes.pop(0) if self.outcomes else JobResult(1, "ok")
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            if self.active_counter is not None:
                self.active_counter["now"] -= 1

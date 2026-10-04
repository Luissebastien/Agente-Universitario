"""The real Jobs wired to the real services (with a fake Moodle client)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from database import scheduler_repository as history
from database.db import connect
from moodle.client import MoodleClient
from notifications.providers import NotificationProvider
from scheduler.app import build_scheduler
from scheduler.config import EXTRACTION, INGESTION, MOODLE_SYNC, NOTIFICATIONS
from scheduler.jobs import (
    CHECKPOINT_OVERLAP_SECONDS,
    TRIGGER_INTERVAL,
    TRIGGER_MANUAL,
    TRIGGER_STARTUP,
    MoodleSyncJob,
    RunContext,
)

from .scheduler_fakes import make_config
from .test_moodle_sync_changes import client_for_cycle


class Recorder(NotificationProvider):
    name = "recorder"

    def __init__(self) -> None:
        self.sent = []

    def send(self, notification) -> None:
        self.sent.append(notification)


class MoodleSyncJobCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def run_job(self, trigger: str):
        with patch("scheduler.jobs.MoodleSync") as sync_cls:
            sync_cls.return_value.sync_changes.return_value = MagicMock(
                items_processed=0, item_warnings=(), summary=lambda: "")
            factory = MagicMock()
            MoodleSyncJob(self.conn, factory).run(RunContext(trigger, lambda: False))
            return sync_cls.return_value.sync_changes.call_args.args[0]

    def test_first_run_ever_is_a_full_sync(self) -> None:
        self.assertIsNone(self.run_job(TRIGGER_INTERVAL))

    def test_startup_is_always_a_full_reconciling_sync(self) -> None:
        history.finish_execution(self.conn, history.start_execution(
            self.conn, MOODLE_SYNC, TRIGGER_INTERVAL, 1, "2026-10-02T10:00:00.000000+00:00"),
            history.STATUS_DONE, "2026-10-02T10:01:00.000000+00:00", 60)
        self.assertIsNone(self.run_job(TRIGGER_STARTUP))

    def test_since_is_the_last_successful_start_minus_overlap(self) -> None:
        started = "2026-10-02T10:00:00.000000+00:00"
        history.finish_execution(self.conn, history.start_execution(
            self.conn, MOODLE_SYNC, TRIGGER_INTERVAL, 1, started),
            history.STATUS_DONE, "2026-10-02T10:01:00.000000+00:00", 60)
        # A later failed attempt must not move the checkpoint.
        history.finish_execution(self.conn, history.start_execution(
            self.conn, MOODLE_SYNC, TRIGGER_INTERVAL, 1, "2026-10-02T16:00:00.000000+00:00"),
            history.STATUS_FAILED, "2026-10-02T16:00:05.000000+00:00", 5, error="x")

        expected = int(datetime.fromisoformat(started).timestamp()) - CHECKPOINT_OVERLAP_SECONDS
        self.assertEqual(self.run_job(TRIGGER_MANUAL), expected)


class EndToEndWiringTests(unittest.TestCase):
    """Startup cycle through the real Jobs and services, fake Moodle only."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(":memory:")
        self.config = make_config(storage_path=Path(self._tmp.name) / "originals", retry_delay_seconds=0)
        self.client = client_for_cycle()
        self.client.__enter__.return_value = self.client
        self.client.download_file.side_effect = lambda url: f"bytes of {url}".encode()
        self.provider = Recorder()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def build(self):
        from extraction.extract import Extraction
        from ingestion.storage import FilesystemStorage
        storage = FilesystemStorage(self.config.storage_path)
        return build_scheduler(self.config, self.conn, client_factory=lambda: self.client,
                               extraction_factory=lambda: Extraction(storage, self.conn),
                               provider=self.provider)

    def statuses(self) -> dict:
        rows = self.conn.execute("SELECT job_name, status FROM scheduler_executions ORDER BY id").fetchall()
        return {r["job_name"]: r["status"] for r in rows}

    def test_startup_runs_the_whole_pipeline_once(self) -> None:
        scheduler = self.build()
        scheduler.startup(one_shot=False)
        while scheduler.step():
            pass

        self.assertEqual(self.statuses(), {MOODLE_SYNC: "done", INGESTION: "done", EXTRACTION: "done"})
        self.assertEqual(self.client.download_file.call_count, 2)  # one file per course
        extracted = self.conn.execute("SELECT COUNT(*) AS n FROM extracted_documents").fetchone()["n"]
        self.assertEqual(extracted, 2)

    def test_second_quiet_cycle_does_not_reingest_or_reextract(self) -> None:
        scheduler = self.build()
        scheduler.startup(one_shot=False)
        while scheduler.step():
            pass
        downloads = self.client.download_file.call_count

        scheduler.request(MOODLE_SYNC, TRIGGER_INTERVAL)
        while scheduler.step():
            pass

        self.assertEqual(self.client.download_file.call_count, downloads)
        counts = self.conn.execute(
            "SELECT job_name, COUNT(*) AS n FROM scheduler_executions GROUP BY job_name").fetchall()
        self.assertEqual({r["job_name"]: r["n"] for r in counts},
                         {MOODLE_SYNC: 2, INGESTION: 1, EXTRACTION: 1})

    def test_moodle_failure_blocks_the_pipeline_and_alerts(self) -> None:
        self.client.get_site_info.side_effect = ConnectionError("unreachable")
        scheduler = self.build()
        scheduler.startup(one_shot=False)
        while scheduler.step():
            pass

        self.assertEqual(self.statuses(), {MOODLE_SYNC: "failed"})
        self.assertEqual([n.notification_type for n in self.provider.sent], ["job_failed"])


if __name__ == "__main__":
    unittest.main()

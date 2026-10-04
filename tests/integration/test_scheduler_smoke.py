"""Gated, READ-ONLY Scheduler smoke test against the real la instancia real Moodle.

Runs one startup cycle (full sync -> ingestion -> extraction -> notifications)
with small budgets in a temp database, then a second, incremental cycle.
Same gate as the other Moodle integration tests: MOODLE_RUN_INTEGRATION_TESTS=1
plus MOODLE_URL/MOODLE_TOKEN. Only read calls and file downloads are made;
nothing is written to Moodle. The token is never printed.
"""
import dataclasses
import os
import tempfile
import unittest
from pathlib import Path

from database.db import connect
from extraction.extract import Extraction
from ingestion.storage import FilesystemStorage
from moodle.client import MoodleClient
from scheduler.app import build_scheduler
from scheduler.config import EXTRACTION, INGESTION, MOODLE_SYNC, BudgetConfig
from scheduler.jobs import TRIGGER_INTERVAL

from tests.unit.scheduler_fakes import make_config

_REQUIRED_VARS = ("MOODLE_URL", "MOODLE_TOKEN")

# Every Moodle web service function the Scheduler may call. All are reads.
READ_ONLY_FUNCTIONS = {
    "core_webservice_get_site_info",
    "core_enrol_get_users_courses",
    "core_course_get_enrolled_courses_by_timeline_classification",
    "core_course_get_updates_since",
    "core_course_get_contents",
    "mod_assign_get_assignments",
    "mod_assign_get_submission_status",
    "gradereport_user_get_grade_items",
    "core_calendar_get_action_events_by_courses",
}


def _integration_enabled() -> bool:
    if os.environ.get("MOODLE_RUN_INTEGRATION_TESTS") != "1":
        return False
    return all(os.environ.get(var) for var in _REQUIRED_VARS)


class _RecordingClient(MoodleClient):
    called: list[str] = []

    def call(self, function_name, params=None):
        type(self).called.append(function_name)
        return super().call(function_name, params)


@unittest.skipUnless(
    _integration_enabled(),
    "Set MOODLE_RUN_INTEGRATION_TESTS=1 plus MOODLE_URL/MOODLE_TOKEN to run against real Moodle",
)
class SchedulerSmokeTest(unittest.TestCase):
    def test_startup_cycle_then_incremental_cycle_read_only(self) -> None:
        _RecordingClient.called = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            conn = connect(Path(tmp_dir) / "smoke.sqlite3")
            try:
                config = make_config(
                    storage_path=Path(tmp_dir) / "originals",
                    retry_delay_seconds=5,
                    ingestion=BudgetConfig(enabled=True, max_items=3, max_seconds=300),
                    extraction=BudgetConfig(enabled=True, max_items=3, max_seconds=300),
                )
                storage = FilesystemStorage(config.storage_path)
                scheduler = build_scheduler(
                    config, conn,
                    client_factory=lambda: _RecordingClient.from_env(),
                    # No OCR model download in a smoke test: native extraction only.
                    extraction_factory=lambda: Extraction(storage, conn),
                )
                scheduler.startup(one_shot=False)
                while scheduler.step():
                    pass

                rows = conn.execute("SELECT * FROM scheduler_executions ORDER BY id").fetchall()
                for r in rows:
                    print(f"[smoke] {r['job_name']}: {r['status']} trigger={r['trigger']} "
                          f"attempt={r['attempt']} items={r['items_processed']} "
                          f"{r['duration_seconds']:.1f}s - {r['detail']}"
                          + (f" | {r['error']}" if r['error'] else ""))
                statuses = {r["job_name"]: r["status"] for r in rows}
                self.assertEqual(statuses.get(MOODLE_SYNC), "done")
                self.assertEqual(statuses.get(INGESTION), "done")
                self.assertEqual(statuses.get(EXTRACTION), "done")
                remaining_versions = conn.execute(
                    "SELECT COUNT(*) AS n FROM resource_versions").fetchone()["n"]
                self.assertLessEqual(remaining_versions, 3)  # ingestion budget respected

                first_cycle_calls = len(_RecordingClient.called)
                scheduler.request(MOODLE_SYNC, TRIGGER_INTERVAL)
                while scheduler.step():
                    pass
                second = conn.execute(
                    "SELECT detail FROM scheduler_executions WHERE job_name = ? ORDER BY id DESC LIMIT 1",
                    (MOODLE_SYNC,)).fetchone()
                print(f"[smoke] second cycle: {second['detail']}")
                self.assertTrue(second["detail"].startswith("incremental"))
                print(f"[smoke] Moodle calls: first cycle {first_cycle_calls}, "
                      f"second cycle {len(_RecordingClient.called) - first_cycle_calls}")

                self.assertTrue(set(_RecordingClient.called) <= READ_ONLY_FUNCTIONS,
                                set(_RecordingClient.called) - READ_ONLY_FUNCTIONS)
                token = os.environ["MOODLE_TOKEN"]
                for r in conn.execute("SELECT detail, error FROM scheduler_executions").fetchall():
                    self.assertNotIn(token, (r["detail"] or "") + (r["error"] or ""))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

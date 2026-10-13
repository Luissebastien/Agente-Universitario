"""The only destructive command in the system.

These tests are mostly about what it must NOT do: run while the scheduler is
writing, delete without being asked twice, delete without a recoverable copy,
or take settings and operational history down with the academic data.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import moodle_repository as repo
from database.db import connect
from moodle.models import Assignment, Course, Grade, MoodleUser
from notifications.reminders import ReminderRule
from scheduler import purge as purge_module
from scheduler.app import EXIT_OK, EXIT_UNAVAILABLE, run_purge
from scheduler.purge import ACADEMIC_TABLES, KEPT_TABLES

from .scheduler_fakes import make_config


def seed(conn: sqlite3.Connection, storage: Path) -> None:
    """A term's worth of data, thin but touching every layer."""
    repo.upsert_user(conn, MoodleUser(id=1, username="u", fullname="Nombre Apellido"))
    repo.upsert_courses(conn, [Course(id=10, shortname="SW", fullname="Ingenieria de Software",
                                      category=None, visible=True, progress=None,
                                      startdate=None, enddate=None)])
    repo.upsert_assignments(conn, [Assignment(id=20, course_id=10, name="Proyecto", duedate=1,
                                              allowsubmissionsfromdate=None, cutoffdate=None,
                                              grade=None)])
    conn.execute(
        "INSERT INTO resources (id, origin, source_type, external_reference, course_id, name,"
        " source_url, created_at, last_synced_at)"
        " VALUES (1,'moodle','file','ref',10,'apuntes.pdf','url','t','t')")
    conn.execute(
        "INSERT INTO resource_versions (id, resource_id, version_number, content_hash,"
        " storage_ref, size_bytes, ingested_at) VALUES (1,1,1,'abc','abc',3,'t')")
    conn.execute(
        "INSERT INTO extracted_documents (resource_version_id, depth, extractor_name,"
        " extractor_version, status, extracted_text, extracted_at)"
        " VALUES (1,'basic','pdf','1','done','texto','t')")
    conn.commit()
    (storage / "abc").write_bytes(b"pdf")


class PurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.storage = self.root / "originals"
        self.storage.mkdir()
        self.backups = self.root / "backups"
        self.conn = connect(self.root / "agente_u.sqlite3")
        seed(self.conn, self.storage)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def rows(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def test_every_academic_table_is_emptied(self) -> None:
        purge_module.purge(self.conn, self.storage, self.backups)

        for table in ACADEMIC_TABLES:
            self.assertEqual(self.rows(table), 0, table)

    def test_the_stored_originals_are_removed(self) -> None:
        report = purge_module.purge(self.conn, self.storage, self.backups)

        self.assertEqual(list(self.storage.iterdir()), [])
        self.assertEqual(report.originals_removed, 1)
        self.assertEqual(report.bytes_freed, 3)

    def test_settings_and_operational_history_survive(self) -> None:
        # The two things a term reset must not take with it: what the student
        # configured, and the record of what the system did.
        from database import reminder_repository as reminders
        reminders.replace_rules(self.conn, (ReminderRule(
            id="3h", offset_seconds=10800,
            title_template="{assignment}", body_template="{course}"),))
        self.conn.execute(
            "INSERT INTO scheduler_executions (job_name, trigger, attempt, status, started_at)"
            " VALUES ('moodle_sync','interval',1,'done','t')")
        self.conn.execute(
            "INSERT INTO notifications_sent (notification_type, subject_key, scheduled_for, sent_at)"
            " VALUES ('assignment_due_24h','assignment:20','t','t')")
        self.conn.commit()

        purge_module.purge(self.conn, self.storage, self.backups)

        self.assertEqual([r.id for r in reminders.get_rules(self.conn)], ["3h"])
        self.assertEqual(self.rows("scheduler_executions"), 1)
        self.assertEqual(self.rows("notifications_sent"), 1)

    def test_the_kept_and_purged_lists_do_not_overlap(self) -> None:
        self.assertEqual(set(ACADEMIC_TABLES) & set(KEPT_TABLES), set())

    def test_the_lists_together_cover_every_table_in_the_schema(self) -> None:
        # Guards against a new table being added and silently belonging to
        # neither list - which is how academic data quietly survives a purge.
        actual = {
            row["name"] for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")
        }
        self.assertEqual(actual, set(ACADEMIC_TABLES) | set(KEPT_TABLES))

    def test_a_recoverable_backup_is_written_before_anything_is_deleted(self) -> None:
        report = purge_module.purge(self.conn, self.storage, self.backups)

        self.assertTrue(report.backup_path.is_file())
        restored = sqlite3.connect(report.backup_path)
        try:
            self.assertEqual(
                restored.execute("SELECT COUNT(*) FROM courses").fetchone()[0], 1)
            self.assertEqual(
                restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            restored.close()


class PurgeCommandTests(unittest.TestCase):
    """The guards around the command, not the deletion itself."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.storage = self.root / "originals"
        self.storage.mkdir()
        self.config = make_config(
            database_path=self.root / "agente_u.sqlite3", storage_path=self.storage
        )
        conn = connect(self.config.database_path)
        seed(conn, self.storage)
        conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def courses(self) -> int:
        conn = connect(self.config.database_path)
        try:
            return conn.execute("SELECT COUNT(*) AS n FROM courses").fetchone()["n"]
        finally:
            conn.close()

    def test_without_yes_it_reports_and_deletes_nothing(self) -> None:
        self.assertEqual(run_purge(self.config, confirmed=False), EXIT_OK)

        self.assertEqual(self.courses(), 1)
        self.assertEqual(list(self.storage.iterdir()), [self.storage / "abc"])

    def test_with_yes_it_deletes(self) -> None:
        self.assertEqual(run_purge(self.config, confirmed=True), EXIT_OK)

        self.assertEqual(self.courses(), 0)

    def test_it_refuses_while_a_scheduler_is_running(self) -> None:
        from scheduler.lock import SingleInstanceLock
        lock = SingleInstanceLock(self.config.lock_path)
        self.assertTrue(lock.try_acquire())
        try:
            result = run_purge(self.config, confirmed=True)
        finally:
            lock.release()

        self.assertEqual(result, EXIT_UNAVAILABLE)
        self.assertEqual(self.courses(), 1)  # untouched

    def test_it_refuses_when_there_is_no_database(self) -> None:
        self.config.database_path.unlink()

        self.assertEqual(run_purge(self.config, confirmed=True), EXIT_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()

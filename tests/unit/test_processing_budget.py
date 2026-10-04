"""Bounded, resumable Ingestion/Extraction runs used by the Scheduler (spec §19, §20)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from database import extraction_repository as extraction_repo
from database import ingestion_repository as repo
from database import moodle_repository as moodle_repo
from database.db import connect
from extraction.extract import MAX_FAILED_ATTEMPTS, Extraction
from extraction.models import ExtractedDocument
from ingestion.ingest import Ingestion
from ingestion.models import ResourceDescriptor, ResourceVersion
from ingestion.source_adapters import MoodleFileSourceAdapter, MoodleUrlSourceAdapter
from ingestion.storage import FilesystemStorage
from moodle.client import MoodleClient
from moodle.exceptions import MoodleAuthenticationError, MoodleConnectionError, MoodleHTTPError
from moodle.models import Course, CourseFile, CourseModule, CourseSection


def file_descriptor(n: int, timemodified: int | None = 100) -> ResourceDescriptor:
    url = f"https://campusvirtual.example.edu/webservice/pluginfile.php/1/f{n}.txt"
    return ResourceDescriptor(origin="moodle", source_type="file", external_reference=url,
                              course_id=None, section_id=None, module_id=None, name=f"f{n}.txt",
                              source_url=url, mimetype="text/plain", source_timemodified=timemodified)


def url_descriptor(target: str) -> ResourceDescriptor:
    return ResourceDescriptor(origin="moodle", source_type="url", external_reference="7",
                              course_id=None, section_id=None, module_id=None, name="Link",
                              source_url=target)


class FakeTicks:
    def __init__(self, step: float) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


class StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = FilesystemStorage(Path(self._tmp.name) / "originals")
        self.client = MagicMock(spec=MoodleClient)
        self.client.download_file.side_effect = lambda url: f"content of {url}".encode()
        self.ingestion = Ingestion(self.client, self.storage, self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()


class IngestionPendingTests(StorageTestCase):
    def test_budget_limits_items_and_the_rest_continues_next_run(self) -> None:
        descriptors = [file_descriptor(n) for n in range(5)]

        first = self.ingestion.ingest_pending(descriptors, max_items=2, max_seconds=999)
        self.assertEqual((first.pending, first.attempted, first.remaining), (5, 2, 3))

        second = self.ingestion.ingest_pending(descriptors, max_items=10, max_seconds=999)
        self.assertEqual((second.pending, second.attempted, second.new_versions), (3, 3, 3))
        self.assertEqual(self.ingestion.pending(descriptors), [])
        self.assertEqual(self.client.download_file.call_count, 5)  # nothing re-downloaded

    def test_time_budget_stops_between_items(self) -> None:
        descriptors = [file_descriptor(n) for n in range(5)]
        result = self.ingestion.ingest_pending(descriptors, max_items=100, max_seconds=2,
                                               monotonic=FakeTicks(step=1.0))
        self.assertLess(result.attempted, 5)
        self.assertEqual(result.remaining, 5 - result.attempted)

    def test_should_stop_ends_the_batch_without_losing_work(self) -> None:
        descriptors = [file_descriptor(n) for n in range(3)]
        result = self.ingestion.ingest_pending(descriptors, 100, 999, should_stop=lambda: True)
        self.assertEqual(result.attempted, 0)
        self.assertEqual(len(self.ingestion.pending(descriptors)), 3)

    def test_unchanged_timemodified_is_not_pending(self) -> None:
        d = file_descriptor(1, timemodified=100)
        self.ingestion.ingest_pending([d], 10, 999)
        self.assertEqual(self.ingestion.pending([d]), [])

    def test_newer_timemodified_with_identical_bytes_is_checked_once(self) -> None:
        # Regression for the design review: comparing Moodle's timemodified to
        # our local ingested_at would re-download identical content forever.
        self.ingestion.ingest_pending([file_descriptor(1, 100)], 10, 999)
        bumped = file_descriptor(1, 200)

        result = self.ingestion.ingest_pending([bumped], 10, 999)
        self.assertEqual((result.attempted, result.new_versions), (1, 0))
        self.assertEqual(self.ingestion.pending([bumped]), [])
        self.assertEqual(self.client.download_file.call_count, 2)

    def test_changed_content_becomes_a_new_current_version_and_history_is_kept(self) -> None:
        self.ingestion.ingest_pending([file_descriptor(1, 100)], 10, 999)
        self.client.download_file.side_effect = lambda url: b"replaced content"
        result = self.ingestion.ingest_pending([file_descriptor(1, 200)], 10, 999)

        self.assertEqual(result.new_versions, 1)
        resource_id = repo.find_resource_state(self.conn, "moodle", "file", file_descriptor(1).external_reference)[0]
        self.assertEqual([v.version_number for v in repo.get_resource_versions(self.conn, resource_id)], [1, 2])

    def test_failed_download_stays_pending_and_does_not_stop_the_batch(self) -> None:
        bad = file_descriptor(1)

        def download(url):
            if url == bad.source_url:
                raise MoodleHTTPError(404)
            return b"ok"

        self.client.download_file.side_effect = download
        result = self.ingestion.ingest_pending([bad, file_descriptor(2)], 10, 999)

        self.assertEqual((result.attempted, result.succeeded, result.failed), (2, 1, 1))
        self.assertIn("404", result.first_error)
        self.assertEqual(self.ingestion.pending([bad, file_descriptor(2)]), [bad])

    def test_failed_download_of_a_changed_file_is_not_lost(self) -> None:
        self.ingestion.ingest_pending([file_descriptor(1, 100)], 10, 999)
        self.client.download_file.side_effect = MoodleConnectionError("network")
        changed = file_descriptor(1, 200)
        self.ingestion.ingest_pending([changed], 10, 999)
        self.assertEqual(self.ingestion.pending([changed]), [changed])

    def test_previously_failed_items_rotate_behind_new_work(self) -> None:
        failing = file_descriptor(1)
        self.client.download_file.side_effect = MoodleHTTPError(404)
        self.ingestion.ingest_pending([failing], 10, 999)

        fresh = file_descriptor(2)
        self.assertEqual(self.ingestion.pending([failing, fresh]), [fresh, failing])

    def test_authentication_failure_is_systemic(self) -> None:
        self.client.download_file.side_effect = MoodleAuthenticationError("invalidtoken", "x")
        with self.assertRaises(Exception) as ctx:
            self.ingestion.ingest_pending([file_descriptor(1), file_descriptor(2)], 10, 999)
        self.assertEqual(self.client.download_file.call_count, 1)
        self.assertIsInstance(ctx.exception.__cause__, MoodleAuthenticationError)

    def test_url_reference_changes_are_pending_without_network(self) -> None:
        self.ingestion.ingest_pending([url_descriptor("https://a.example")], 10, 999)
        self.assertEqual(self.ingestion.pending([url_descriptor("https://a.example")]), [])
        repointed = url_descriptor("https://b.example")
        self.assertEqual(self.ingestion.pending([repointed]), [repointed])
        self.client.download_file.assert_not_called()


class StaleSourceRowTests(unittest.TestCase):
    """Rows Moodle stopped returning are never deleted; adapters must not offer them."""

    def setUp(self) -> None:
        self.conn = connect(":memory:")
        moodle_repo.upsert_courses(self.conn, [Course(id=1, shortname="c", fullname="C", category=None,
                                                      visible=True, progress=None, startdate=None, enddate=None)])

    def tearDown(self) -> None:
        self.conn.close()

    def sync(self, files: list[str], url_module: bool) -> None:
        moodle_repo.upsert_sections(self.conn, [CourseSection(id=10, course_id=1, section_number=0,
                                                               name="General", summary="", visible=True)])
        modules = [CourseModule(id=100, course_id=1, section_id=10, modname="resource", name="R",
                                url=None, visible=True)]
        if url_module:
            modules.append(CourseModule(id=101, course_id=1, section_id=10, modname="url", name="U",
                                        url="https://x.example", visible=True))
        moodle_repo.upsert_modules(self.conn, modules)
        moodle_repo.upsert_files(self.conn, [CourseFile(module_id=100, filename=f, filepath="/", filesize=1,
                                                        mimetype=None, fileurl=f"https://h/{f}", timemodified=1)
                                             for f in files])

    def test_file_and_url_rows_missing_from_the_latest_sync_are_not_offered(self) -> None:
        self.sync(["old.pdf"], url_module=True)
        # Age the first sync's rows, then a later sync no longer returns old.pdf or the URL module.
        self.conn.execute("UPDATE course_files SET last_synced_at = '2000-01-01T00:00:00+00:00'")
        self.conn.execute("UPDATE course_modules SET last_synced_at = '2000-01-01T00:00:00+00:00' WHERE id = 101")
        self.conn.commit()
        self.sync(["new.pdf"], url_module=False)

        names = [d.name for d in MoodleFileSourceAdapter().discover(self.conn)]
        self.assertEqual(names, ["new.pdf"])
        self.assertEqual(list(MoodleUrlSourceAdapter().discover(self.conn)), [])

    def test_adapter_exposes_moodle_timemodified(self) -> None:
        self.sync(["a.pdf"], url_module=False)
        [descriptor] = MoodleFileSourceAdapter().discover(self.conn)
        self.assertEqual(descriptor.source_timemodified, 1)


class ExtractionPendingTests(StorageTestCase):
    def add_version(self, n: int, version_number: int = 1, content: bytes = b"hello") -> ResourceVersion:
        resource = repo.upsert_resource(self.conn, file_descriptor(n))
        ref = self.storage.store(content, f"h{n}-{version_number}")
        return repo.insert_resource_version(self.conn, ResourceVersion(
            id=None, resource_id=resource.id, version_number=version_number, content_hash=f"h{n}-{version_number}",
            storage_ref=ref, size_bytes=len(content), mimetype="text/plain", ingested_at="2026-01-01T00:00:00+00:00"))

    def test_budget_limits_and_remaining_work_continues(self) -> None:
        for n in range(4):
            self.add_version(n)
        extraction = Extraction(self.storage, self.conn)

        first = extraction.extract_pending("basic", max_items=3, max_seconds=999)
        self.assertEqual((first.pending, first.attempted, first.done, first.remaining), (4, 3, 3, 1))
        second = extraction.extract_pending("basic", max_items=3, max_seconds=999)
        self.assertEqual((second.pending, second.attempted), (1, 1))
        self.assertEqual(extraction.pending_version_ids("basic"), [])

    def test_only_the_current_version_is_processed_history_is_kept(self) -> None:
        old = self.add_version(1, version_number=1)
        current = self.add_version(1, version_number=2, content=b"new")
        extraction = Extraction(self.storage, self.conn)

        self.assertEqual(extraction.pending_version_ids("basic"), [current.id])
        extraction.extract_pending("basic", 10, 999)
        self.assertEqual(extraction_repo.get_extracted_documents(self.conn, old.id), [])
        self.assertIsNotNone(repo.get_resource_version(self.conn, old.id))

    def _failed(self, version_id: int, extractor_name: str = "plain_text") -> None:
        extraction_repo.insert_extracted_document(self.conn, ExtractedDocument(
            id=None, resource_version_id=version_id, depth="basic", extractor_name=extractor_name,
            extractor_version="1", status="failed", error_reason="boom", extracted_text=None))

    def test_transient_failures_are_retried_a_bounded_number_of_times(self) -> None:
        version = self.add_version(1)
        extraction = Extraction(self.storage, self.conn)
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            self._failed(version.id)
        self.assertEqual(extraction.pending_version_ids("basic"), [version.id])
        self._failed(version.id)
        self.assertEqual(extraction.pending_version_ids("basic"), [])

    def test_unsupported_format_is_never_retried(self) -> None:
        version = self.add_version(1)
        self._failed(version.id, extractor_name="unsupported")
        self.assertEqual(Extraction(self.storage, self.conn).pending_version_ids("basic"), [])

    def test_unexpected_item_error_is_recorded_and_the_batch_continues(self) -> None:
        bad = self.add_version(1)
        good = self.add_version(2)
        extraction = Extraction(self.storage, self.conn)
        original = extraction.extract

        def extract(version_id, depth="basic"):
            if version_id == bad.id:
                raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
            return original(version_id, depth)

        extraction.extract = extract
        result = extraction.extract_pending("basic", 10, 999)

        self.assertEqual((result.attempted, result.done, result.failed), (2, 1, 1))
        [row] = extraction_repo.get_extracted_documents(self.conn, bad.id)
        self.assertEqual(row.status, "failed")
        self.assertIn("UnicodeEncodeError", row.error_reason)
        self.assertEqual(extraction_repo.get_extracted_documents(self.conn, good.id)[0].status, "done")

    def test_database_errors_propagate(self) -> None:
        self.add_version(1)
        extraction = Extraction(self.storage, self.conn)
        extraction.extract = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))
        with self.assertRaises(sqlite3.OperationalError):
            extraction.extract_pending("basic", 10, 999)


if __name__ == "__main__":
    unittest.main()

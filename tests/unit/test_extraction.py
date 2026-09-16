import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from database import extraction_repository as extraction_repo
from database import ingestion_repository as ingestion_repo
from database.db import connect
from extraction.extract import Extraction, ExtractionError
from ingestion.models import ResourceDescriptor, ResourceVersion
from ingestion.storage import FilesystemStorage, StorageError, StorageNotFoundError


def _descriptor(name: str, mimetype: str | None) -> ResourceDescriptor:
    return ResourceDescriptor(
        origin="moodle",
        source_type="file",
        external_reference=f"https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/{name}",
        course_id=None,
        section_id=None,
        module_id=None,
        name=name,
        source_url=f"https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/{name}",
        mimetype=mimetype,
    )


class ExtractionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = FilesystemStorage(Path(self._tmp.name) / "originals")
        self.extraction = Extraction(self.storage, self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _ingest(self, name: str, mimetype: str | None, content: bytes) -> ResourceVersion:
        resource = ingestion_repo.upsert_resource(self.conn, _descriptor(name, mimetype))
        storage_ref = self.storage.store(content, "irrelevant-in-these-tests")
        return ingestion_repo.insert_resource_version(
            self.conn,
            ResourceVersion(
                id=None,
                resource_id=resource.id,
                version_number=1,
                content_hash="irrelevant-in-these-tests",
                storage_ref=storage_ref,
                size_bytes=len(content),
                mimetype=mimetype,
                ingested_at="2026-01-01T00:00:00+00:00",
            ),
        )


class SupportedFormatTests(ExtractionTestCase):
    def test_extracts_plain_text(self) -> None:
        version = self._ingest("notes.txt", "text/plain", b"hello world")

        doc = self.extraction.extract(version.id)

        self.assertEqual(doc.status, "done")
        self.assertEqual(doc.extracted_text, "hello world")
        self.assertEqual(doc.extractor_name, "plain_text")
        self.assertIsNone(doc.error_reason)

    def test_extracts_html_visible_text(self) -> None:
        version = self._ingest("page.html", "text/html", b"<p>Hola</p>")

        doc = self.extraction.extract(version.id)

        self.assertEqual(doc.status, "done")
        self.assertEqual(doc.extracted_text, "Hola")
        self.assertEqual(doc.extractor_name, "html_text")

    def test_falls_back_to_extension_when_mimetype_is_none(self) -> None:
        # Mirrors a real observed case: Moodle sometimes reports no mimetype at all.
        version = self._ingest("page.html", None, b"<p>Sin mimetype</p>")

        doc = self.extraction.extract(version.id)

        self.assertEqual(doc.status, "done")
        self.assertEqual(doc.extractor_name, "html_text")

    def test_persists_the_attempt(self) -> None:
        version = self._ingest("notes.txt", "text/plain", b"hello world")
        doc = self.extraction.extract(version.id)

        stored = extraction_repo.get_extracted_documents(self.conn, version.id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].id, doc.id)


class UnsupportedFormatTests(ExtractionTestCase):
    def test_unsupported_mimetype_fails_explicitly_without_raising(self) -> None:
        version = self._ingest("slides.pptx", "application/vnd.ms-powerpoint", b"\x00\x01binary")

        doc = self.extraction.extract(version.id)

        self.assertEqual(doc.status, "failed")
        self.assertIsNone(doc.extracted_text)
        self.assertIn("unsupported format", doc.error_reason)

    def test_unsupported_format_does_not_delete_the_original(self) -> None:
        version = self._ingest("slides.pptx", "application/vnd.ms-powerpoint", b"\x00\x01binary")
        self.extraction.extract(version.id)

        self.assertTrue(self.storage.exists(version.storage_ref))
        self.assertEqual(self.storage.retrieve(version.storage_ref), b"\x00\x01binary")


class ContentFailureTests(ExtractionTestCase):
    def test_undecodable_declared_text_fails_explicitly(self) -> None:
        version = self._ingest("mislabeled.txt", "text/plain", b"\xff\xfe\x00\x01")

        doc = self.extraction.extract(version.id)

        self.assertEqual(doc.status, "failed")
        self.assertIn("UnicodeDecodeError", doc.error_reason)

    def test_storage_retrieval_failure_fails_explicitly_without_raising(self) -> None:
        resource = ingestion_repo.upsert_resource(self.conn, _descriptor("gone.txt", "text/plain"))
        version = ingestion_repo.insert_resource_version(
            self.conn,
            ResourceVersion(
                id=None,
                resource_id=resource.id,
                version_number=1,
                content_hash="never-stored",
                storage_ref="never-stored",
                size_bytes=0,
                mimetype="text/plain",
                ingested_at="2026-01-01T00:00:00+00:00",
            ),
        )

        doc = self.extraction.extract(version.id)

        self.assertEqual(doc.status, "failed")
        self.assertIn("retrieve", doc.error_reason)


class InvalidRequestTests(ExtractionTestCase):
    def test_unknown_resource_version_raises(self) -> None:
        with self.assertRaises(ExtractionError):
            self.extraction.extract(999999)

    def test_invalid_depth_raises(self) -> None:
        version = self._ingest("notes.txt", "text/plain", b"hi")
        with self.assertRaises(ExtractionError):
            self.extraction.extract(version.id, depth="ultra")


class AppendOnlyTests(ExtractionTestCase):
    def test_re_extracting_creates_a_new_attempt_not_a_dedup(self) -> None:
        version = self._ingest("notes.txt", "text/plain", b"hello world")

        first = self.extraction.extract(version.id)
        second = self.extraction.extract(version.id)

        self.assertNotEqual(first.id, second.id)
        attempts = extraction_repo.get_extracted_documents(self.conn, version.id)
        self.assertEqual(len(attempts), 2)


class DepthTests(ExtractionTestCase):
    def test_depth_is_recorded_on_the_document(self) -> None:
        version = self._ingest("notes.txt", "text/plain", b"hello world")

        basic = self.extraction.extract(version.id, depth="basic")
        deep = self.extraction.extract(version.id, depth="deep")

        self.assertEqual(basic.depth, "basic")
        self.assertEqual(deep.depth, "deep")


if __name__ == "__main__":
    unittest.main()

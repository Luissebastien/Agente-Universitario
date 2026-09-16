import unittest

from database import extraction_repository as repo
from database import ingestion_repository as ingestion_repo
from database.db import connect
from extraction.models import ExtractedDocument
from ingestion.models import ResourceDescriptor, ResourceVersion

DESCRIPTOR = ResourceDescriptor(
    origin="moodle",
    source_type="file",
    external_reference="https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/x.txt",
    course_id=None,
    section_id=None,
    module_id=None,
    name="x.txt",
    source_url="https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/x.txt",
    mimetype="text/plain",
)


class ExtractionRepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        resource = ingestion_repo.upsert_resource(self.conn, DESCRIPTOR)
        version = ingestion_repo.insert_resource_version(
            self.conn,
            ResourceVersion(
                id=None,
                resource_id=resource.id,
                version_number=1,
                content_hash="hash1",
                storage_ref="hash1",
                size_bytes=5,
                mimetype="text/plain",
                ingested_at="2026-01-01T00:00:00+00:00",
            ),
        )
        self.version_id = version.id

    def tearDown(self) -> None:
        self.conn.close()

    def _doc(self, status: str = "done", extracted_text: str | None = "hello") -> ExtractedDocument:
        return ExtractedDocument(
            id=None,
            resource_version_id=self.version_id,
            depth="basic",
            extractor_name="plain_text",
            extractor_version="1",
            status=status,
            error_reason=None if status == "done" else "boom",
            extracted_text=extracted_text if status == "done" else None,
            metadata={"char_count": 5},
            extracted_at="2026-01-01T00:00:00+00:00",
        )


class InsertTests(ExtractionRepositoryTestCase):
    def test_insert_assigns_id(self) -> None:
        inserted = repo.insert_extracted_document(self.conn, self._doc())
        self.assertIsNotNone(inserted.id)

    def test_insert_preserves_metadata_round_trip(self) -> None:
        inserted = repo.insert_extracted_document(self.conn, self._doc())
        self.assertEqual(inserted.metadata, {"char_count": 5})

    def test_insert_twice_creates_two_rows_append_only(self) -> None:
        repo.insert_extracted_document(self.conn, self._doc())
        repo.insert_extracted_document(self.conn, self._doc())

        docs = repo.get_extracted_documents(self.conn, self.version_id)
        self.assertEqual(len(docs), 2)


class GetLatestExtractionTests(ExtractionRepositoryTestCase):
    def test_none_when_no_attempts(self) -> None:
        self.assertIsNone(repo.get_latest_extraction(self.conn, self.version_id, "basic"))

    def test_returns_most_recent_attempt(self) -> None:
        repo.insert_extracted_document(self.conn, self._doc(status="failed"))
        second = repo.insert_extracted_document(self.conn, self._doc(status="done"))

        latest = repo.get_latest_extraction(self.conn, self.version_id, "basic")
        self.assertEqual(latest.id, second.id)
        self.assertEqual(latest.status, "done")

    def test_scoped_by_depth(self) -> None:
        repo.insert_extracted_document(self.conn, self._doc())
        deep_doc = ExtractedDocument(
            id=None,
            resource_version_id=self.version_id,
            depth="deep",
            extractor_name="plain_text",
            extractor_version="1",
            status="done",
            error_reason=None,
            extracted_text="hello",
            metadata={},
            extracted_at="2026-01-01T00:00:00+00:00",
        )
        repo.insert_extracted_document(self.conn, deep_doc)

        basic = repo.get_latest_extraction(self.conn, self.version_id, "basic")
        deep = repo.get_latest_extraction(self.conn, self.version_id, "deep")
        self.assertEqual(basic.depth, "basic")
        self.assertEqual(deep.depth, "deep")


class FailedAttemptTests(ExtractionRepositoryTestCase):
    def test_failed_attempt_preserves_error_reason_and_no_text(self) -> None:
        inserted = repo.insert_extracted_document(self.conn, self._doc(status="failed"))
        self.assertEqual(inserted.status, "failed")
        self.assertEqual(inserted.error_reason, "boom")
        self.assertIsNone(inserted.extracted_text)


if __name__ == "__main__":
    unittest.main()

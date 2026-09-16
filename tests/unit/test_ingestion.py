import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from database.db import connect
from database import ingestion_repository as repo
from database import moodle_repository as moodle_repo
from ingestion.ingest import Ingestion, IngestionError
from ingestion.models import ResourceDescriptor
from ingestion.storage import FilesystemStorage, StorageError
from moodle.client import MoodleClient
from moodle.exceptions import MoodleConnectionError
from moodle.models import Course, CourseModule, CourseSection

FILE_DESCRIPTOR = ResourceDescriptor(
    origin="moodle",
    source_type="file",
    external_reference="https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/x.pdf",
    course_id=4409,
    section_id=501,
    module_id=905633,
    name="x.pdf",
    source_url="https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/x.pdf",
    mimetype="application/pdf",
)

URL_DESCRIPTOR = ResourceDescriptor(
    origin="moodle",
    source_type="url",
    external_reference="1",
    course_id=4409,
    section_id=501,
    module_id=1,
    name="Calendario",
    source_url="https://www.intec.edu.do/calendarios",
)

INCOMPLETE_DESCRIPTOR = ResourceDescriptor(
    origin="moodle",
    source_type="file",
    external_reference="https://campusvirtual.intec.edu.do/webservice/pluginfile.php/2/y.bin",
    course_id=None,
    section_id=None,
    module_id=None,
    name="y.bin",
    source_url="https://campusvirtual.intec.edu.do/webservice/pluginfile.php/2/y.bin",
    mimetype=None,  # Moodle did not report a mimetype
)


def make_client(content: bytes = b"%PDF-1.4 fake pdf") -> MagicMock:
    client = MagicMock(spec=MoodleClient)
    client.download_file.return_value = content
    return client


class IngestionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = FilesystemStorage(Path(self._tmp.name) / "originals")

        # Provenance fixtures: FILE_DESCRIPTOR/URL_DESCRIPTOR reference real
        # course/section/module rows, matching the FK constraints resources
        # actually has (mirroring what MoodleSync would have already synced).
        moodle_repo.upsert_courses(
            self.conn,
            [Course(id=4409, shortname="a", fullname="A", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )
        moodle_repo.upsert_sections(
            self.conn,
            [CourseSection(id=501, course_id=4409, section_number=1, name="Semana 1",
                            summary="", visible=True)],
        )
        moodle_repo.upsert_modules(
            self.conn,
            [
                CourseModule(id=905633, course_id=4409, section_id=501, modname="resource",
                             name="x.pdf", url=None, visible=True),
                CourseModule(id=1, course_id=4409, section_id=501, modname="url",
                             name="Calendario", url="https://www.intec.edu.do/calendarios",
                             visible=True),
            ],
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()


class NewResourceTests(IngestionTestCase):
    def test_ingesting_a_new_file_creates_version_1(self) -> None:
        client = make_client(b"content-v1")
        ingestion = Ingestion(client, self.storage, self.conn)

        version = ingestion.ingest(FILE_DESCRIPTOR)

        self.assertEqual(version.version_number, 1)
        self.assertEqual(self.storage.retrieve(version.storage_ref), b"content-v1")
        client.download_file.assert_called_once_with(FILE_DESCRIPTOR.source_url)

    def test_ingesting_a_new_url_stores_the_reference_only(self) -> None:
        client = make_client()
        ingestion = Ingestion(client, self.storage, self.conn)

        version = ingestion.ingest(URL_DESCRIPTOR)

        self.assertEqual(version.version_number, 1)
        self.assertEqual(
            self.storage.retrieve(version.storage_ref), URL_DESCRIPTOR.source_url.encode("utf-8")
        )
        self.assertEqual(version.mimetype, "text/uri-list")
        client.download_file.assert_not_called()  # no crawling of the target


class ExistingResourceTests(IngestionTestCase):
    def test_unchanged_content_does_not_create_a_new_version(self) -> None:
        client = make_client(b"same bytes")
        ingestion = Ingestion(client, self.storage, self.conn)

        first = ingestion.ingest(FILE_DESCRIPTOR)
        second = ingestion.ingest(FILE_DESCRIPTOR)
        third = ingestion.ingest(FILE_DESCRIPTOR)

        self.assertEqual(first.id, second.id)
        self.assertEqual(second.id, third.id)
        resource = repo.upsert_resource(self.conn, FILE_DESCRIPTOR)
        self.assertEqual(len(repo.get_resource_versions(self.conn, resource.id)), 1)

    def test_changed_content_creates_version_2_without_touching_version_1(self) -> None:
        client = make_client(b"version one")
        ingestion = Ingestion(client, self.storage, self.conn)
        v1 = ingestion.ingest(FILE_DESCRIPTOR)

        client.download_file.return_value = b"version two - different"
        v2 = ingestion.ingest(FILE_DESCRIPTOR)

        self.assertEqual(v1.version_number, 1)
        self.assertEqual(v2.version_number, 2)
        self.assertNotEqual(v1.content_hash, v2.content_hash)

        # v1's stored bytes remain exactly as they were.
        self.assertEqual(self.storage.retrieve(v1.storage_ref), b"version one")
        self.assertEqual(self.storage.retrieve(v2.storage_ref), b"version two - different")

        resource = repo.upsert_resource(self.conn, FILE_DESCRIPTOR)
        self.assertEqual(len(repo.get_resource_versions(self.conn, resource.id)), 2)


class UrlVersioningTests(IngestionTestCase):
    """URL versions track the preserved reference, never remote content.

    Ingestion never fetches what a URL points to, so these tests only ever
    assert on the reference string itself - not on any notion of "the
    destination's content changed", which this layer cannot and does not
    observe.
    """

    def test_ingesting_the_same_url_twice_is_idempotent(self) -> None:
        client = make_client()
        ingestion = Ingestion(client, self.storage, self.conn)

        first = ingestion.ingest(URL_DESCRIPTOR)
        second = ingestion.ingest(URL_DESCRIPTOR)

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.version_number, 1)
        resource = repo.upsert_resource(self.conn, URL_DESCRIPTOR)
        self.assertEqual(len(repo.get_resource_versions(self.conn, resource.id)), 1)
        client.download_file.assert_not_called()

    def test_changing_the_referenced_url_creates_a_new_version(self) -> None:
        client = make_client()
        ingestion = Ingestion(client, self.storage, self.conn)
        v1 = ingestion.ingest(URL_DESCRIPTOR)

        # Same module (same external_reference/identity), Moodle now points
        # it at a different address - this is a reference change, not a
        # claim that anything changed on the destination page.
        repointed = ResourceDescriptor(
            origin=URL_DESCRIPTOR.origin,
            source_type=URL_DESCRIPTOR.source_type,
            external_reference=URL_DESCRIPTOR.external_reference,
            course_id=URL_DESCRIPTOR.course_id,
            section_id=URL_DESCRIPTOR.section_id,
            module_id=URL_DESCRIPTOR.module_id,
            name=URL_DESCRIPTOR.name,
            source_url="https://www.intec.edu.do/calendarios-2027",
        )
        v2 = ingestion.ingest(repointed)

        self.assertEqual(v1.version_number, 1)
        self.assertEqual(v2.version_number, 2)
        self.assertNotEqual(v1.content_hash, v2.content_hash)
        self.assertEqual(self.storage.retrieve(v1.storage_ref), URL_DESCRIPTOR.source_url.encode("utf-8"))
        self.assertEqual(self.storage.retrieve(v2.storage_ref), repointed.source_url.encode("utf-8"))
        client.download_file.assert_not_called()  # still never fetched


class FailureTests(IngestionTestCase):
    def test_download_failure_raises_and_creates_no_version(self) -> None:
        client = MagicMock(spec=MoodleClient)
        client.download_file.side_effect = MoodleConnectionError("network unreachable")
        ingestion = Ingestion(client, self.storage, self.conn)

        with self.assertRaises(IngestionError):
            ingestion.ingest(FILE_DESCRIPTOR)

        resource = repo.upsert_resource(self.conn, FILE_DESCRIPTOR)
        self.assertIsNone(repo.get_latest_version(self.conn, resource.id))

    def test_storage_failure_raises_and_creates_no_version(self) -> None:
        client = make_client(b"content")
        broken_storage = MagicMock(spec=FilesystemStorage)
        broken_storage.store.side_effect = StorageError("disk full")
        ingestion = Ingestion(client, broken_storage, self.conn)

        with self.assertRaises(IngestionError):
            ingestion.ingest(FILE_DESCRIPTOR)

        resource = repo.upsert_resource(self.conn, FILE_DESCRIPTOR)
        self.assertIsNone(repo.get_latest_version(self.conn, resource.id))

    def test_retry_after_failure_succeeds_and_creates_exactly_one_version(self) -> None:
        client = MagicMock(spec=MoodleClient)
        client.download_file.side_effect = [MoodleConnectionError("timeout"), b"recovered content"]
        ingestion = Ingestion(client, self.storage, self.conn)

        with self.assertRaises(IngestionError):
            ingestion.ingest(FILE_DESCRIPTOR)

        version = ingestion.ingest(FILE_DESCRIPTOR)  # retry

        self.assertEqual(version.version_number, 1)
        resource = repo.upsert_resource(self.conn, FILE_DESCRIPTOR)
        self.assertEqual(len(repo.get_resource_versions(self.conn, resource.id)), 1)


class IncompleteMetadataTests(IngestionTestCase):
    def test_missing_mimetype_is_stored_as_none_not_guessed(self) -> None:
        client = make_client(b"binary content")
        ingestion = Ingestion(client, self.storage, self.conn)

        version = ingestion.ingest(INCOMPLETE_DESCRIPTOR)

        self.assertIsNone(version.mimetype)


class ProvenanceTests(IngestionTestCase):
    def test_resource_and_version_preserve_full_provenance_chain(self) -> None:
        client = make_client(b"content")
        ingestion = Ingestion(client, self.storage, self.conn)

        version = ingestion.ingest(FILE_DESCRIPTOR)
        resource = repo.get_resource(self.conn, version.resource_id)

        self.assertEqual(resource.origin, "moodle")
        self.assertEqual(resource.source_type, "file")
        self.assertEqual(resource.course_id, 4409)
        self.assertEqual(resource.section_id, 501)
        self.assertEqual(resource.module_id, 905633)
        self.assertEqual(resource.source_url, FILE_DESCRIPTOR.source_url)
        self.assertEqual(resource.external_reference, FILE_DESCRIPTOR.external_reference)


if __name__ == "__main__":
    unittest.main()

import unittest

from database.db import connect
from database import ingestion_repository as repo
from database import moodle_repository as moodle_repo
from ingestion.models import ResourceDescriptor, ResourceVersion
from moodle.models import Course

DESCRIPTOR = ResourceDescriptor(
    origin="moodle",
    source_type="file",
    external_reference="https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/x.pdf",
    course_id=None,
    section_id=None,
    module_id=None,
    name="x.pdf",
    source_url="https://campusvirtual.intec.edu.do/webservice/pluginfile.php/1/x.pdf",
    mimetype="application/pdf",
)


class IngestionRepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()


class UpsertResourceTests(IngestionRepositoryTestCase):
    def test_creates_resource_with_assigned_id(self) -> None:
        resource = repo.upsert_resource(self.conn, DESCRIPTOR)
        self.assertIsNotNone(resource.id)
        self.assertEqual(resource.name, "x.pdf")

    def test_upsert_twice_is_idempotent_identity(self) -> None:
        first = repo.upsert_resource(self.conn, DESCRIPTOR)
        second = repo.upsert_resource(self.conn, DESCRIPTOR)

        self.assertEqual(first.id, second.id)
        rows = self.conn.execute("SELECT * FROM resources").fetchall()
        self.assertEqual(len(rows), 1)

    def test_upsert_refreshes_descriptive_fields(self) -> None:
        repo.upsert_resource(self.conn, DESCRIPTOR)
        moodle_repo.upsert_courses(
            self.conn,
            [Course(id=42, shortname="b", fullname="B", category=None, visible=True,
                    progress=None, startdate=None, enddate=None)],
        )

        renamed = ResourceDescriptor(
            origin=DESCRIPTOR.origin,
            source_type=DESCRIPTOR.source_type,
            external_reference=DESCRIPTOR.external_reference,
            course_id=42,
            section_id=DESCRIPTOR.section_id,
            module_id=DESCRIPTOR.module_id,
            name="renamed.pdf",
            source_url=DESCRIPTOR.source_url,
            mimetype=DESCRIPTOR.mimetype,
        )
        updated = repo.upsert_resource(self.conn, renamed)

        self.assertEqual(updated.name, "renamed.pdf")
        self.assertEqual(updated.course_id, 42)

    def test_different_source_type_is_a_different_resource(self) -> None:
        url_descriptor = ResourceDescriptor(
            origin="moodle",
            source_type="url",
            external_reference=DESCRIPTOR.external_reference,  # same string, different source_type
            course_id=None,
            section_id=None,
            module_id=None,
            name="link",
            source_url="https://example.com",
        )
        a = repo.upsert_resource(self.conn, DESCRIPTOR)
        b = repo.upsert_resource(self.conn, url_descriptor)

        self.assertNotEqual(a.id, b.id)


class VersioningTests(IngestionRepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.resource = repo.upsert_resource(self.conn, DESCRIPTOR)

    def _version(self, number: int, content_hash: str) -> ResourceVersion:
        return ResourceVersion(
            id=None,
            resource_id=self.resource.id,
            version_number=number,
            content_hash=content_hash,
            storage_ref=content_hash,
            size_bytes=10,
            mimetype="application/pdf",
            ingested_at="2026-01-01T00:00:00+00:00",
        )

    def test_no_version_initially(self) -> None:
        self.assertIsNone(repo.get_latest_version(self.conn, self.resource.id))

    def test_insert_version_1(self) -> None:
        inserted = repo.insert_resource_version(self.conn, self._version(1, "hashA"))
        self.assertIsNotNone(inserted.id)

        latest = repo.get_latest_version(self.conn, self.resource.id)
        self.assertEqual(latest.version_number, 1)
        self.assertEqual(latest.content_hash, "hashA")

    def test_second_version_does_not_touch_first(self) -> None:
        repo.insert_resource_version(self.conn, self._version(1, "hashA"))
        repo.insert_resource_version(self.conn, self._version(2, "hashB"))

        versions = repo.get_resource_versions(self.conn, self.resource.id)
        self.assertEqual([v.version_number for v in versions], [1, 2])
        self.assertEqual(versions[0].content_hash, "hashA")  # untouched

        latest = repo.get_latest_version(self.conn, self.resource.id)
        self.assertEqual(latest.version_number, 2)


if __name__ == "__main__":
    unittest.main()

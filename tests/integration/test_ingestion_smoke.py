import os
import tempfile
import unittest
from pathlib import Path

from database.db import connect
from ingestion.ingest import Ingestion
from ingestion.source_adapters import MoodleFileSourceAdapter, MoodleUrlSourceAdapter
from ingestion.storage import FilesystemStorage
from moodle.client import MoodleClient
from moodle.sync import MoodleSync

_REQUIRED_VARS = ("MOODLE_URL", "MOODLE_TOKEN")


def _integration_enabled() -> bool:
    if os.environ.get("MOODLE_RUN_INTEGRATION_TESTS") != "1":
        return False
    return all(os.environ.get(var) for var in _REQUIRED_VARS)


@unittest.skipUnless(
    _integration_enabled(),
    "Set MOODLE_RUN_INTEGRATION_TESTS=1 plus MOODLE_URL/MOODLE_TOKEN to run against real Moodle",
)
class IngestionSmokeTest(unittest.TestCase):
    def test_ingest_real_files_and_urls_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            conn = connect(Path(tmp_dir) / "smoke.sqlite3")
            storage = FilesystemStorage(Path(tmp_dir) / "originals")
            try:
                with MoodleClient.from_env() as client:
                    sync = MoodleSync(client, conn)
                    user = sync.sync_profile()
                    courses = sync.sync_courses(user.id)
                    self.assertGreater(len(courses), 0)

                    # Sync contents for a couple of courses so adapters have data.
                    for course in courses[:2]:
                        sync.sync_course_contents(course.id)

                    ingestion = Ingestion(client, storage, conn)

                    file_descriptors = list(MoodleFileSourceAdapter().discover(conn))
                    url_descriptors = list(MoodleUrlSourceAdapter().discover(conn))
                    print(
                        f"[smoke] discovered {len(file_descriptors)} file(s), "
                        f"{len(url_descriptors)} url(s)"
                    )

                    ingested_versions = []
                    if file_descriptors:
                        # Keep this smoke test light: ingest one real, small-ish file.
                        smallest = min(
                            (d for d in file_descriptors), key=lambda d: d.name
                        )
                        v1 = ingestion.ingest(smallest)
                        v1_again = ingestion.ingest(smallest)  # idempotency against real content
                        self.assertEqual(v1.id, v1_again.id)
                        self.assertEqual(v1.version_number, 1)
                        self.assertTrue(storage.exists(v1.storage_ref))
                        print(
                            f"[smoke] ingested file '{smallest.name}': "
                            f"{v1.size_bytes} bytes, hash={v1.content_hash[:12]}..."
                        )
                        ingested_versions.append(v1)

                    if url_descriptors:
                        u1 = ingestion.ingest(url_descriptors[0])
                        u1_again = ingestion.ingest(url_descriptors[0])
                        self.assertEqual(u1.id, u1_again.id)
                        print(f"[smoke] ingested url '{url_descriptors[0].name}'")
                        ingested_versions.append(u1)

                    self.assertGreater(len(ingested_versions), 0, "expected at least one resource to ingest")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

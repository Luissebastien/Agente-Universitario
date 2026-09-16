import os
import tempfile
import unittest
from pathlib import Path

from database.db import connect
from extraction.extract import Extraction
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
class ExtractionSmokeTest(unittest.TestCase):
    def test_extract_real_ingested_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            conn = connect(Path(tmp_dir) / "smoke.sqlite3")
            storage = FilesystemStorage(Path(tmp_dir) / "originals")
            try:
                with MoodleClient.from_env() as client:
                    sync = MoodleSync(client, conn)
                    user = sync.sync_profile()
                    courses = sync.sync_courses(user.id)
                    self.assertGreater(len(courses), 0)

                    for course in courses[:2]:
                        sync.sync_course_contents(course.id)

                    ingestion = Ingestion(client, storage, conn)
                    extraction = Extraction(storage, conn)

                    file_descriptors = list(MoodleFileSourceAdapter().discover(conn))
                    url_descriptors = list(MoodleUrlSourceAdapter().discover(conn))

                    results = []
                    if file_descriptors:
                        smallest = min(file_descriptors, key=lambda d: d.name)
                        version = ingestion.ingest(smallest)
                        doc = extraction.extract(version.id)
                        print(
                            f"[smoke] extracted file '{smallest.name}' "
                            f"(mimetype={version.mimetype!r}): status={doc.status}, "
                            f"extractor={doc.extractor_name}"
                        )
                        results.append(doc)

                    if url_descriptors:
                        version = ingestion.ingest(url_descriptors[0])
                        doc = extraction.extract(version.id)
                        print(
                            f"[smoke] extracted url '{url_descriptors[0].name}': "
                            f"status={doc.status}, extracted_text={doc.extracted_text!r}"
                        )
                        # URL references are preserved text/uri-list content, so
                        # this format is always supported.
                        self.assertEqual(doc.status, "done")
                        self.assertEqual(doc.extracted_text, url_descriptors[0].source_url)
                        results.append(doc)

                    self.assertGreater(len(results), 0, "expected at least one resource to process")
                    # Every attempt (success or explicit unsupported-format failure)
                    # must be recorded - never silently dropped.
                    for doc in results:
                        self.assertIn(doc.status, {"done", "failed"})
                        if doc.status == "failed":
                            self.assertIsNotNone(doc.error_reason)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

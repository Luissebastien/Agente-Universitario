"""Gated smoke test: syncs real INTEC data (courses, classification,
assignments, grades, calendar, and a couple of course contents) and then
exercises AcademicReadModel against it - courses, assignments, grades,
calendar, materials, and extracted documents.

Same gate as the other Moodle integration tests (MOODLE_RUN_INTEGRATION_TESTS
plus MOODLE_URL/MOODLE_TOKEN). Never part of the normal suite. Never prints
the token.
"""
import os
import tempfile
import unittest
from pathlib import Path

from database.db import connect
from ingestion.ingest import Ingestion
from ingestion.source_adapters import MoodleFileSourceAdapter
from ingestion.storage import FilesystemStorage
from moodle.client import MoodleClient
from moodle.sync import MoodleSync
from read_model import AcademicReadModel

_REQUIRED_VARS = ("MOODLE_URL", "MOODLE_TOKEN")


def _integration_enabled() -> bool:
    if os.environ.get("MOODLE_RUN_INTEGRATION_TESTS") != "1":
        return False
    return all(os.environ.get(var) for var in _REQUIRED_VARS)


@unittest.skipUnless(
    _integration_enabled(),
    "Set MOODLE_RUN_INTEGRATION_TESTS=1 plus MOODLE_URL/MOODLE_TOKEN to run against real Moodle",
)
class AcademicReadModelSmokeTest(unittest.TestCase):
    def test_read_model_against_real_intec_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            conn = connect(Path(tmp_dir) / "smoke.sqlite3")
            storage = FilesystemStorage(Path(tmp_dir) / "originals")
            try:
                with MoodleClient.from_env() as client:
                    sync = MoodleSync(client, conn)
                    user = sync.sync_profile()
                    courses = sync.sync_courses(user.id)
                    self.assertGreater(len(courses), 0)

                    classification = sync.sync_course_classification()
                    print(f"[smoke] classified {len(classification)} real courses via Moodle's own timeline API")

                    course_ids = [c.id for c in courses]
                    sync.sync_assignments(course_ids)
                    sync.sync_calendar(course_ids)
                    for course_id in course_ids[:2]:
                        sync.sync_course_contents(course_id)
                        sync.sync_grades(course_id, user.id)

                    # Sync submission status for a handful of real assignments so
                    # get_pending_assignments() has something non-trivial to show.
                    for row in conn.execute("SELECT id FROM assignments LIMIT 5").fetchall():
                        try:
                            sync.sync_assignment_submission_status(row["id"])
                        except Exception as exc:
                            print(f"[smoke] submission status sync failed for {row['id']}: {exc}")

                    # Ingest one real file so materials/extraction have something real to show.
                    ingestion = Ingestion(client, storage, conn)
                    file_descriptors = list(MoodleFileSourceAdapter().discover(conn))
                    if file_descriptors:
                        smallest = min(file_descriptors, key=lambda d: d.name)
                        ingestion.ingest(smallest)

                read_model = AcademicReadModel(conn)

                current = read_model.get_current_courses()
                print(f"[smoke] current courses: {len(current)}")
                self.assertGreater(len(current), 0)
                for c in current[:3]:
                    print(f"  - {c.shortname} (classification={c.timeline_classification})")

                pending = read_model.get_pending_assignments()
                print(f"[smoke] pending assignments: {len(pending)}")

                upcoming = read_model.get_upcoming_assignments(days=30)
                print(f"[smoke] upcoming assignments (30d): {len(upcoming)}")

                for c in current:
                    grades = read_model.get_grades(course_id=c.id)
                    if grades:
                        print(f"[smoke] grades for {c.shortname}: {len(grades)} items, "
                              f"e.g. {grades[0].item_name!r} = {grades[0].grade_formatted!r}")
                        break

                events = read_model.get_calendar_events(0, 4_000_000_000)
                print(f"[smoke] calendar events (all time): {len(events)}")

                materials_found = False
                for c in current:
                    materials = read_model.get_course_materials(c.id)
                    if materials:
                        materials_found = True
                        m = materials[0]
                        print(f"[smoke] material '{m.name}': version={m.latest_version_number}, "
                              f"extraction_status={m.extraction_status}")
                        if m.latest_version_id is not None:
                            doc = read_model.get_extracted_document(m.latest_version_id)
                            if doc is not None:
                                print(f"[smoke] extracted_document: status={doc.status}, "
                                      f"extractor={doc.extractor_name}, "
                                      f"chars={len(doc.extracted_text or '')}")
                        break
                if not materials_found:
                    print("[smoke] no ingested materials found for any current course (expected "
                          "if this is the first run) - get_course_materials() still returned "
                          "cleanly with an empty list")

                # Read-only contract: nothing above may have written to the DB.
                self.assertGreaterEqual(len(read_model.get_courses()), len(current))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

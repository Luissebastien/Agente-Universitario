import io
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


def _ocr_integration_enabled() -> bool:
    return os.environ.get("RUN_OCR_INTEGRATION_TESTS") == "1"


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


def _local_fixture(kind: str) -> bytes:
    """Builds a tiny, valid document locally for formats not present in the
    real INTEC courses at the time this test was written (confirmed absent
    via the extraction-strategy benchmark: PPTX, XLS, ODT, ODS). Still
    exercises the real Ingestion -> Storage -> Extraction pipeline end to
    end, just without a real Moodle download for that one format."""
    if kind == "pptx":
        import pptx
        from pptx.util import Inches

        p = pptx.Presentation()
        slide = p.slides.add_slide(p.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
        box.text_frame.text = "Fixture local de PPTX"
        buf = io.BytesIO()
        p.save(buf)
        return buf.getvalue()
    if kind == "odt":
        from odf.opendocument import OpenDocumentText
        from odf.text import P

        doc = OpenDocumentText()
        doc.text.addElement(P(text="Fixture local de ODT"))
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()
    if kind == "ods":
        from odf.opendocument import OpenDocumentSpreadsheet
        from odf.table import Table, TableCell, TableRow
        from odf.text import P

        doc = OpenDocumentSpreadsheet()
        table = Table(name="Datos")
        row = TableRow()
        cell = TableCell()
        cell.addElement(P(text="Fixture local de ODS"))
        row.addElement(cell)
        table.addElement(row)
        doc.spreadsheet.addElement(table)
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()
    raise ValueError(f"no local fixture builder for {kind!r}")


@unittest.skipUnless(
    _integration_enabled() and _ocr_integration_enabled(),
    "Set MOODLE_RUN_INTEGRATION_TESTS=1 and RUN_OCR_INTEGRATION_TESTS=1 "
    "(plus MOODLE_URL/MOODLE_TOKEN) for the full multi-format pipeline validation",
)
class ExtractionMultiFormatPipelineTest(unittest.TestCase):
    """Validates Moodle -> Ingestion -> Storage -> Extraction -> extracted_documents
    across every MVP format, using real INTEC resources where they exist and
    a local fixture (routed through the same real pipeline) where they don't.
    """

    def test_pipeline_across_mvp_formats(self) -> None:
        from extraction.ocr import DoctrOcrEngine

        with tempfile.TemporaryDirectory() as tmp_dir:
            conn = connect(Path(tmp_dir) / "smoke.sqlite3")
            storage = FilesystemStorage(Path(tmp_dir) / "originals")
            try:
                with MoodleClient.from_env() as client:
                    sync = MoodleSync(client, conn)
                    user = sync.sync_profile()
                    courses = sync.sync_courses(user.id)
                    self.assertGreater(len(courses), 0)
                    for course in courses:
                        sync.sync_course_contents(course.id)

                    ingestion = Ingestion(client, storage, conn)
                    extraction = Extraction(storage, conn, ocr_engine=DoctrOcrEngine())

                    file_descriptors = list(MoodleFileSourceAdapter().discover(conn))
                    by_ext: dict[str, list] = {}
                    for d in file_descriptors:
                        ext = Path(d.name).suffix.lower()
                        by_ext.setdefault(ext, []).append(d)

                    results = {}

                    # --- Real INTEC resources, per format present ---
                    real_targets = {
                        "pdf": [".pdf"], "docx": [".docx"], "ppsx": [".ppsx"],
                        "xlsx": [".xlsx"], "image": [".png", ".jpg", ".jpeg"],
                    }
                    for label, exts in real_targets.items():
                        candidates = [d for ext in exts for d in by_ext.get(ext, [])]
                        if not candidates:
                            print(f"[smoke] {label}: NOT FOUND in real INTEC courses, skipping")
                            continue
                        smallest = min(candidates, key=lambda d: d.name)
                        version = ingestion.ingest(smallest)
                        doc = extraction.extract(version.id)
                        print(
                            f"[smoke] {label} (real INTEC '{smallest.name}'): status={doc.status}, "
                            f"extractor={doc.extractor_name}, method={doc.metadata.get('method')}, "
                            f"chars={len(doc.extracted_text or '')}"
                        )
                        results[label] = doc

                    # --- HTML (Moodle page/book content) ---
                    html_candidates = [d for d in by_ext.get(".html", [])]
                    if html_candidates:
                        version = ingestion.ingest(html_candidates[0])
                        doc = extraction.extract(version.id)
                        print(f"[smoke] html: status={doc.status}, extractor={doc.extractor_name}")
                        results["html"] = doc
                    else:
                        print("[smoke] html: NOT FOUND in real INTEC courses, skipping")

                    # --- Local fixtures for formats confirmed absent from real INTEC ---
                    for kind, mimetype in [
                        ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
                        ("odt", "application/vnd.oasis.opendocument.text"),
                        ("ods", "application/vnd.oasis.opendocument.spreadsheet"),
                    ]:
                        if kind in by_ext:
                            continue  # real one exists after all, skip the fixture
                        data = _local_fixture(kind)
                        storage_ref = storage.store(data, f"smoke-fixture-{kind}")
                        from ingestion.models import ResourceDescriptor

                        descriptor = ResourceDescriptor(
                            origin="local_fixture", source_type="file",
                            external_reference=f"fixture://{kind}", course_id=None, section_id=None,
                            module_id=None, name=f"fixture.{kind}",
                            source_url=f"fixture://{kind}", mimetype=mimetype,
                        )
                        from database import ingestion_repository as ingestion_repo
                        from ingestion.models import ResourceVersion
                        import hashlib
                        from datetime import datetime, timezone

                        resource = ingestion_repo.upsert_resource(conn, descriptor)
                        version = ingestion_repo.insert_resource_version(
                            conn,
                            ResourceVersion(
                                id=None, resource_id=resource.id, version_number=1,
                                content_hash=hashlib.sha256(data).hexdigest(), storage_ref=storage_ref,
                                size_bytes=len(data), mimetype=mimetype,
                                ingested_at=datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                        doc = extraction.extract(version.id)
                        print(
                            f"[smoke] {kind} (LOCAL FIXTURE, not present in real INTEC): "
                            f"status={doc.status}, extractor={doc.extractor_name}"
                        )
                        results[kind] = doc

                    self.assertGreater(len(results), 0, "expected at least one format to be validated")
                    for label, doc in results.items():
                        self.assertIn(doc.status, {"done", "failed"}, f"{label} produced an invalid status")
                        if doc.status == "failed":
                            self.assertIsNotNone(doc.error_reason, f"{label} failed without a reason")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

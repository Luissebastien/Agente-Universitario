"""Resource limits: the bounds that keep one hostile or accidental document
from consuming the whole machine, and the visibility that makes a skipped
file reviewable instead of invisible.

Thresholds were set from the real corpus (199 files: median 0.09 MB,
p99 11.42 MB, largest 34.24 MB; heaviest realistic document a 786-page
textbook at 5.93 MB), so no real file is affected by any of them.
"""
import io
import tempfile
import tracemalloc
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from database import extraction_repository as extraction_repo
from database import ingestion_repository as repo
from database.db import connect
from extraction import ocr as ocr_module
from extraction.extract import Extraction
from extraction.extractors import (
    MAX_RENDER_PIXELS,
    OCR_RENDER_DPI,
    ExtractionInterrupted,
    PdfExtractor,
    _render_scale,
)
from extraction.ocr import ImageTooLargeError, OcrEngine, OcrResult, deskew, estimate_skew_angle
from ingestion.ingest import Ingestion
from ingestion.models import ResourceDescriptor, ResourceVersion
from ingestion.storage import FilesystemStorage
from moodle.client import MoodleClient
from moodle.exceptions import MoodleConnectionError, MoodleResourceTooLargeError
from moodle.models import Course

FAKE_TOKEN = "0123456789abcdef0123456789abcdef"
HOST = "https://campusvirtual.example.edu"
COURSE_ID = 101


def seed_course(conn) -> None:
    """resources.course_id is a real foreign key and PRAGMA foreign_keys is ON."""
    from database import moodle_repository as moodle_repo

    moodle_repo.upsert_courses(conn, [Course(
        id=COURSE_ID, shortname="C", fullname="Curso", category=None,
        visible=True, progress=None, startdate=None, enddate=None)])


def descriptor(name: str = "big.pdf", timemodified: int | None = 100) -> ResourceDescriptor:
    url = f"{HOST}/webservice/pluginfile.php/1/{name}"
    return ResourceDescriptor(
        origin="moodle", source_type="file", external_reference=url, course_id=COURSE_ID,
        section_id=None, module_id=None, name=name, source_url=url,
        mimetype="application/pdf", source_timemodified=timemodified,
    )


class _FakeOcrEngine(OcrEngine):
    name = "fake"
    version = "1"

    def __init__(self) -> None:
        self.calls = 0

    def ocr(self, image_bytes: bytes) -> OcrResult:
        self.calls += 1
        return OcrResult(text="page text")


# --------------------------------------------------------------------------
# E - the download limit itself
# --------------------------------------------------------------------------

class DownloadLimitTests(unittest.TestCase):
    LIMIT = 1000

    def client(self) -> MoodleClient:
        return MoodleClient(HOST, FAKE_TOKEN, max_resource_bytes=self.LIMIT)

    @patch("moodle.client.urllib.request.urlopen")
    def test_declared_length_over_the_limit_never_reads_the_body(
        self, mock_urlopen: MagicMock
    ) -> None:
        """The whole point of checking Content-Length: an oversized body must
        not reach memory at all."""
        response = MagicMock()
        response.headers.get.return_value = str(self.LIMIT + 1)
        mock_urlopen.return_value.__enter__.return_value = response

        with self.assertRaises(MoodleResourceTooLargeError) as caught:
            self.client().download_file(f"{HOST}/webservice/pluginfile.php/1/x.pdf")

        response.read.assert_not_called()
        self.assertEqual(caught.exception.size_bytes, self.LIMIT + 1)
        self.assertEqual(caught.exception.limit_bytes, self.LIMIT)

    @patch("moodle.client.urllib.request.urlopen")
    def test_undeclared_length_is_still_bounded(self, mock_urlopen: MagicMock) -> None:
        """A body that declares no size (or lies) is read one byte past the
        limit and refused - never unbounded."""
        response = MagicMock()
        response.headers.get.return_value = None
        response.read.return_value = b"x" * (self.LIMIT + 1)
        mock_urlopen.return_value.__enter__.return_value = response

        with self.assertRaises(MoodleResourceTooLargeError) as caught:
            self.client().download_file(f"{HOST}/webservice/pluginfile.php/1/x.pdf")

        response.read.assert_called_once_with(self.LIMIT + 1)
        self.assertIsNone(caught.exception.size_bytes)  # size was never declared

    @patch("moodle.client.urllib.request.urlopen")
    def test_a_file_exactly_at_the_limit_is_downloaded(self, mock_urlopen: MagicMock) -> None:
        response = MagicMock()
        response.headers.get.return_value = str(self.LIMIT)
        response.read.return_value = b"y" * self.LIMIT
        mock_urlopen.return_value.__enter__.return_value = response

        data = self.client().download_file(f"{HOST}/webservice/pluginfile.php/1/x.pdf")

        self.assertEqual(len(data), self.LIMIT)

    @patch("moodle.client.urllib.request.urlopen")
    def test_message_leaks_neither_token_nor_url(self, mock_urlopen: MagicMock) -> None:
        """This message reaches logs, execution history and notifications."""
        response = MagicMock()
        response.headers.get.return_value = str(self.LIMIT + 1)
        mock_urlopen.return_value.__enter__.return_value = response

        with self.assertRaises(MoodleResourceTooLargeError) as caught:
            self.client().download_file(f"{HOST}/webservice/pluginfile.php/1/secret.pdf")

        message = str(caught.exception)
        self.assertNotIn(FAKE_TOKEN, message)
        self.assertNotIn("pluginfile", message)
        self.assertNotIn("secret.pdf", message)


# --------------------------------------------------------------------------
# E - what happens afterwards: recorded, skipped, reviewable
# --------------------------------------------------------------------------

class DeferralTests(unittest.TestCase):
    LIMIT = 64 * 1024 * 1024

    def setUp(self) -> None:
        self.conn = connect(":memory:")
        seed_course(self.conn)
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = FilesystemStorage(Path(self._tmp.name) / "originals")
        self.client = MagicMock(spec=MoodleClient)
        self.ingestion = Ingestion(self.client, self.storage, self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def oversized(self, size: int = 100 * 1024 * 1024) -> None:
        self.client.download_file.side_effect = MoodleResourceTooLargeError(size, self.LIMIT)

    def run_once(self, *descriptors) -> object:
        return self.ingestion.ingest_pending(list(descriptors), max_items=10, max_seconds=999)

    def test_oversized_file_is_deferred_not_failed(self) -> None:
        self.oversized()
        result = self.run_once(descriptor())

        self.assertEqual((result.deferred, result.failed, result.succeeded), (1, 0, 0))
        versions = self.conn.execute("SELECT COUNT(*) n FROM resource_versions").fetchone()["n"]
        self.assertEqual(versions, 0)  # nothing was stored

    def test_the_size_and_the_limit_in_force_are_both_recorded(self) -> None:
        """Without these two numbers the limit cannot be retuned from real data."""
        self.oversized(size=120 * 1024 * 1024)
        self.run_once(descriptor())

        [row] = repo.list_deferrals(self.conn)
        self.assertEqual(row["reason"], repo.REASON_OVERSIZED)
        self.assertEqual(row["size_bytes"], 120 * 1024 * 1024)
        self.assertEqual(row["limit_bytes"], self.LIMIT)
        self.assertEqual(row["source_timemodified"], 100)
        self.assertEqual(row["name"], "big.pdf")      # enough provenance to act on
        self.assertEqual(row["course_id"], COURSE_ID)
        self.assertIsNotNone(row["deferred_at"])

    def test_a_deferred_file_is_not_offered_again(self) -> None:
        """The reason the limit is usable at all: no re-download every cycle."""
        self.oversized()
        self.run_once(descriptor())
        self.client.download_file.reset_mock()

        self.assertEqual(self.ingestion.pending([descriptor()]), [])
        second = self.run_once(descriptor())
        self.assertEqual((second.pending, second.attempted), (0, 0))
        self.client.download_file.assert_not_called()

    def test_a_new_source_version_is_evaluated_again(self) -> None:
        """If the teacher replaces the file, the old decision no longer applies."""
        self.oversized()
        self.run_once(descriptor(timemodified=100))

        replaced = descriptor(timemodified=200)
        self.assertEqual(self.ingestion.pending([replaced]), [replaced])
        again = self.run_once(replaced)
        self.assertEqual(again.deferred, 1)
        [row] = repo.list_deferrals(self.conn)
        self.assertEqual(row["source_timemodified"], 200)

    def test_an_ordinary_download_failure_never_defers(self) -> None:
        """Only a size decision stops the retries; a network error must not."""
        self.client.download_file.side_effect = MoodleConnectionError("network down")
        result = self.run_once(descriptor())

        self.assertEqual((result.failed, result.deferred), (1, 0))
        self.assertEqual(repo.list_deferrals(self.conn), [])
        self.assertEqual(self.ingestion.pending([descriptor()]), [descriptor()])

    def test_a_deferral_is_cleared_once_the_file_is_ingested(self) -> None:
        """E.g. the limit was raised, or a smaller replacement arrived: the
        list must not keep claiming the file was skipped."""
        self.oversized()
        self.run_once(descriptor(timemodified=100))

        self.client.download_file.side_effect = None
        self.client.download_file.return_value = b"%PDF-1.4 small now"
        self.run_once(descriptor(timemodified=200))

        self.assertEqual(repo.list_deferrals(self.conn), [])

    def test_the_job_detail_mentions_deferred_items(self) -> None:
        """So a skipped file shows up in scheduler history, not only in a log line."""
        from scheduler.config import BudgetConfig
        from scheduler.jobs import IngestionJob, RunContext

        self.oversized()
        job = IngestionJob(self.conn, self.storage, lambda: self.client,
                           BudgetConfig(enabled=True, max_items=10, max_seconds=999))
        job._descriptors = lambda: [descriptor()]  # type: ignore[method-assign]
        self.client.__enter__ = MagicMock(return_value=self.client)
        self.client.__exit__ = MagicMock(return_value=False)

        result = job.run(RunContext("manual", lambda: False))

        self.assertIn("1 deferred (too large)", result.detail)


# --------------------------------------------------------------------------
# F - image pixels, and the reordering that bounds deskew memory
# --------------------------------------------------------------------------

def png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def barred_image(side: int = 600, skew: float = 0.0) -> Image.Image:
    """Text-like horizontal bars, optionally rotated by a known angle.

    A featureless image gives the skew search nothing to maximise, so these
    tests need real structure to assert anything meaningful about the angle.
    """
    image = Image.new("L", (side, side), 255)
    for y in range(side // 15, side - side // 15, side // 20):
        for x in range(side // 10, side - side // 10):
            for dy in range(max(1, side // 100)):
                image.putpixel((x, y + dy), 0)
    if skew:
        image = image.rotate(skew, expand=True, fillcolor=255)
    return image.convert("RGB")


class ImageLimitTests(unittest.TestCase):
    def test_an_image_over_the_pixel_limit_is_refused_explicitly(self) -> None:
        """Refused, not silently passed through: the OCR engine downstream has
        no limit of its own."""
        data = png_bytes(Image.new("RGB", (40, 40), (255, 255, 255)))
        with patch.object(ocr_module, "MAX_IMAGE_PIXELS", 100):
            with self.assertRaises(ImageTooLargeError) as caught:
                deskew(data)
        self.assertIn("1600 pixels", str(caught.exception))

    def test_an_already_straight_image_is_returned_unchanged(self) -> None:
        """Within the limit and with no real skew, re-encoding would only lose
        quality, so the original bytes come back."""
        data = png_bytes(barred_image(side=200))
        self.assertEqual(deskew(data), data)

    def test_undecodable_bytes_are_still_passed_through_untouched(self) -> None:
        self.assertEqual(deskew(b"not an image at all"), b"not an image at all")

    def test_a_known_skew_is_still_recovered_after_the_reordering(self) -> None:
        """The reduction now happens before thresholding, so the mask is built
        from resampled pixels. The estimate must still find the real angle."""
        angle = estimate_skew_angle(barred_image(skew=-6.0))

        self.assertAlmostEqual(angle, 6.0, delta=1.0)

    def test_deskew_memory_no_longer_scales_with_the_image(self) -> None:
        """Regression guard for the reordering: the old code allocated float64
        at full resolution (~25 bytes/pixel, ~2.2 GB for an image Pillow
        accepts with only a warning). Peak must now be flat."""
        peaks = []
        for side in (1000, 3000):              # 1 MP vs 9 MP
            image = Image.new("RGB", (side, side), (255, 255, 255))
            tracemalloc.start()
            estimate_skew_angle(image)
            peaks.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()

        self.assertLess(peaks[1], peaks[0] * 3)  # was ~9x with the old ordering


# --------------------------------------------------------------------------
# F3 - rendered pixels per PDF page (the page decides its own size, not us)
# --------------------------------------------------------------------------

class RenderScaleTests(unittest.TestCase):
    BASE = OCR_RENDER_DPI / 72

    def test_normal_pages_render_at_the_benchmark_resolution(self) -> None:
        for label, (w, h) in {"Letter": (612, 792), "A4": (595, 842)}.items():
            with self.subTest(page=label):
                self.assertEqual(_render_scale(w, h), self.BASE)

    def test_an_enormous_page_is_rendered_smaller_instead_of_failing(self) -> None:
        """14400 pt is the largest MediaBox the PDF format allows: 1,600 MP
        and ~4.8 GB of RGB at full resolution."""
        scale = _render_scale(14400, 14400)

        self.assertLess(scale, self.BASE)
        self.assertLessEqual(int(14400 * scale) * int(14400 * scale), MAX_RENDER_PIXELS)

    def test_a_degenerate_page_size_does_not_divide_by_zero(self) -> None:
        self.assertEqual(_render_scale(0, 0), self.BASE)


# --------------------------------------------------------------------------
# G - no page or time cap, but interruptible without losing the document
# --------------------------------------------------------------------------

def fake_pdf_page() -> MagicMock:
    page = MagicMock()
    page.get_size.return_value = (612.0, 792.0)
    pixmap = MagicMock()
    pixmap.to_pil.return_value = Image.new("RGB", (10, 10), (255, 255, 255))
    page.render.return_value = pixmap
    return page


def patched_pdf(n_pages: int):
    reader = MagicMock()
    reader.pages = [MagicMock(**{"extract_text.return_value": ""}) for _ in range(n_pages)]
    document = MagicMock()
    document.__iter__.return_value = iter([fake_pdf_page() for _ in range(n_pages)])
    return (
        patch("pypdf.PdfReader", return_value=reader),
        patch("pypdfium2.PdfDocument", return_value=document),
    )


class LongDocumentTests(unittest.TestCase):
    def test_a_long_scan_is_not_truncated_by_any_page_cap(self) -> None:
        """Professors post whole books; a page cap would mutilate them."""
        engine = _FakeOcrEngine()
        reader_patch, document_patch = patched_pdf(300)
        with reader_patch, document_patch:
            result = PdfExtractor(ocr_engine=engine).extract(b"fake pdf")

        self.assertEqual(engine.calls, 300)
        self.assertEqual(result.metadata["pages"], 300)
        self.assertEqual(result.metadata["pages_rendered_below_target_dpi"], 0)

    def test_a_stop_request_interrupts_between_pages(self) -> None:
        engine = _FakeOcrEngine()
        reader_patch, document_patch = patched_pdf(100)
        with reader_patch, document_patch:
            with self.assertRaises(ExtractionInterrupted):
                PdfExtractor(ocr_engine=engine).extract(
                    b"fake pdf", should_stop=lambda: engine.calls >= 3
                )

        self.assertEqual(engine.calls, 3)  # stopped promptly, not at the end


class InterruptedExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        seed_course(self.conn)
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = FilesystemStorage(Path(self._tmp.name) / "originals")
        content = b"%PDF-1.4 scanned"
        ref = self.storage.store(content, "hash-of-scan")
        repo.upsert_resource(self.conn, descriptor("scan.pdf"))
        self.version = repo.insert_resource_version(self.conn, ResourceVersion(
            id=None, resource_id=1, version_number=1, content_hash="hash-of-scan",
            storage_ref=ref, size_bytes=len(content), mimetype="application/pdf",
            ingested_at="2026-10-04T00:00:00+00:00"))

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def extraction(self) -> Extraction:
        return Extraction(self.storage, self.conn, ocr_engine=_FakeOcrEngine())

    def test_an_interrupted_document_records_nothing_at_all(self) -> None:
        """A partial text stored as 'done' would be truncated forever, and a
        'failed' row would count against the bounded retry - three shutdowns
        during the same book would then abandon it."""
        reader_patch, document_patch = patched_pdf(50)
        with reader_patch, document_patch:
            with self.assertRaises(ExtractionInterrupted):
                self.extraction().extract(self.version.id, "basic", lambda: True)

        self.assertEqual(extraction_repo.get_extracted_documents(self.conn, self.version.id), [])

    def test_the_version_stays_pending_after_an_interruption(self) -> None:
        reader_patch, document_patch = patched_pdf(50)
        extraction = self.extraction()
        with reader_patch, document_patch:
            result = extraction.extract_pending("basic", 10, 999, should_stop=lambda: True)

        self.assertEqual((result.attempted, result.done, result.failed), (0, 0, 0))
        self.assertEqual(extraction.pending_version_ids("basic"), [self.version.id])


# --------------------------------------------------------------------------
# L - zip members, because every Office document is a zip container
# --------------------------------------------------------------------------

def office_zip(members: dict[str, bytes], prefix: str = "word/media/") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, payload in members.items():
            z.writestr(prefix + name, payload)
    return buf.getvalue()


class ZipMemberLimitTests(unittest.TestCase):
    def ocr_embedded(self, data: bytes):
        return ocr_module.ocr_embedded_images(data, "word/media/", _FakeOcrEngine())

    def test_an_oversized_member_is_skipped_and_reported(self) -> None:
        data = office_zip({"a.png": png_bytes(Image.new("RGB", (20, 20), (0, 0, 0)))})
        with patch.object(ocr_module, "MAX_ZIP_MEMBER_BYTES", 10):
            texts, per_image = self.ocr_embedded(data)

        self.assertEqual(texts, [])
        self.assertEqual(per_image[0]["status"], "skipped")
        self.assertIn("over the 10-byte limit", per_image[0]["reason"])

    def test_a_highly_compressible_member_is_skipped(self) -> None:
        """A zip bomb's signature: tiny compressed, enormous inflated."""
        data = office_zip({"bomb.png": b"\0" * 2_000_000})
        with patch.object(ocr_module, "MAX_ZIP_COMPRESSION_RATIO", 50):
            texts, per_image = self.ocr_embedded(data)

        self.assertEqual(texts, [])
        self.assertIn("compression ratio", per_image[0]["reason"])

    def test_the_per_document_total_stops_further_members(self) -> None:
        image = png_bytes(Image.new("RGB", (20, 20), (0, 0, 0)))
        data = office_zip({"a.png": image, "b.png": image, "c.png": image})
        with patch.object(ocr_module, "MAX_ZIP_TOTAL_BYTES", len(image) + 1):
            _, per_image = self.ocr_embedded(data)

        statuses = [entry.get("status") for entry in per_image]
        self.assertEqual(statuses.count("done"), 1)
        self.assertEqual(statuses.count("skipped"), 2)
        self.assertIn("total for one document", per_image[1]["reason"])

    def test_a_safe_member_is_still_processed(self) -> None:
        data = office_zip({"a.png": png_bytes(Image.new("RGB", (20, 20), (0, 0, 0)))})
        texts, per_image = self.ocr_embedded(data)

        self.assertEqual(per_image[0]["status"], "done")
        self.assertEqual(texts, ["[word/media/a.png]\npage text"])

    def test_an_oversized_slide_is_skipped_without_losing_the_others(self) -> None:
        slides = {
            "slide1.xml": b'<?xml version="1.0"?><p><t>first</t></p>',
            "slide2.xml": b'<?xml version="1.0"?><p><t>' + b"second" * 2000 + b"</t></p>",
        }
        data = office_zip(slides, prefix="ppt/slides/")
        with patch.object(ocr_module, "MAX_ZIP_MEMBER_BYTES", 100):
            text, n_slides = ocr_module.extract_ooxml_slide_text(data)

        self.assertEqual(n_slides, 2)       # both were seen
        self.assertIn("first", text)        # the safe one was read
        self.assertNotIn("second", text)    # the oversized one was not


if __name__ == "__main__":
    unittest.main()

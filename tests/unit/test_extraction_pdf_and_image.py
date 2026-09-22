import io
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from extraction.extractors import ImageExtractor, PdfExtractor, is_native_text_sufficient
from extraction.ocr import OcrEngine, OcrResult


class _FakeOcrEngine(OcrEngine):
    name = "fake"
    version = "1"

    def __init__(self, text: str = "ocr recovered text"):
        self._text = text
        self.calls = 0

    def ocr(self, image_bytes: bytes) -> OcrResult:
        self.calls += 1
        return OcrResult(text=self._text, metadata={})


def _mock_reader(pages_text: list[str]):
    """A MagicMock standing in for pypdf.PdfReader(...)."""
    pages = []
    for text in pages_text:
        page = MagicMock()
        page.extract_text.return_value = text
        pages.append(page)
    reader = MagicMock()
    reader.pages = pages
    return reader


class IsNativeTextSufficientTests(unittest.TestCase):
    def test_empty_list_is_insufficient(self) -> None:
        self.assertFalse(is_native_text_sufficient([]))

    def test_all_pages_well_above_threshold_is_sufficient(self) -> None:
        self.assertTrue(is_native_text_sufficient(["x" * 200] * 5))

    def test_all_pages_empty_is_insufficient(self) -> None:
        self.assertFalse(is_native_text_sufficient(["", "", ""]))

    def test_uses_median_not_mean_a_few_image_only_pages_dont_trigger_ocr(self) -> None:
        # 8 text-heavy pages, 2 image-only pages - median stays well above threshold.
        pages = ["x" * 300] * 8 + ["", ""]
        self.assertTrue(is_native_text_sufficient(pages))

    def test_uses_median_not_mean_a_few_text_pages_dont_mask_a_scan(self) -> None:
        # 1 text-heavy cover page, 9 empty scanned pages - median is near zero.
        pages = ["x" * 500] + [""] * 9
        self.assertFalse(is_native_text_sufficient(pages))

    def test_custom_threshold_is_respected(self) -> None:
        pages = ["x" * 30] * 3
        self.assertFalse(is_native_text_sufficient(pages, min_chars_per_page=50))
        self.assertTrue(is_native_text_sufficient(pages, min_chars_per_page=20))


class PdfExtractorNativeTests(unittest.TestCase):
    def test_sufficient_native_text_skips_ocr_entirely(self) -> None:
        engine = _FakeOcrEngine()
        extractor = PdfExtractor(ocr_engine=engine)
        with patch("pypdf.PdfReader", return_value=_mock_reader(["x" * 200] * 3)):
            result = extractor.extract(b"fake pdf bytes")

        self.assertEqual(result.metadata["method"], "native")
        self.assertTrue(result.metadata["sufficient_text"])
        self.assertEqual(engine.calls, 0)  # OCR never invoked
        self.assertIn("x" * 200, result.text)

    def test_native_text_joins_all_pages_in_order(self) -> None:
        extractor = PdfExtractor(ocr_engine=None)
        with patch("pypdf.PdfReader", return_value=_mock_reader(["a" * 100, "b" * 100])):
            result = extractor.extract(b"fake pdf bytes")
        self.assertEqual(result.text, "a" * 100 + "\n" + "b" * 100)


class PdfExtractorOcrFallbackTests(unittest.TestCase):
    def test_insufficient_text_with_no_ocr_engine_completes_with_scanned_flag(self) -> None:
        # DEC-048: "scanned PDF without OCR -> extraction may complete with
        # an explicit `scanned` flag and no extracted text" - not a failure.
        extractor = PdfExtractor(ocr_engine=None)
        with patch("pypdf.PdfReader", return_value=_mock_reader(["", "", ""])):
            result = extractor.extract(b"fake pdf bytes")

        self.assertEqual(result.text, "")
        self.assertTrue(result.metadata["scanned"])
        self.assertFalse(result.metadata["sufficient_text"])
        self.assertFalse(result.metadata["ocr_available"])

    def test_insufficient_text_with_ocr_engine_falls_back_to_ocr(self) -> None:
        engine = _FakeOcrEngine(text="ocr text")
        extractor = PdfExtractor(ocr_engine=engine)

        fake_page = MagicMock()
        fake_pixmap = MagicMock()
        fake_pixmap.to_pil.return_value = Image.new("RGB", (10, 10))
        fake_page.render.return_value = fake_pixmap
        fake_pdf = MagicMock()
        fake_pdf.__iter__.return_value = iter([fake_page, fake_page])
        fake_pdf.close = MagicMock()

        with patch("pypdf.PdfReader", return_value=_mock_reader(["", ""])), \
             patch("pypdfium2.PdfDocument", return_value=fake_pdf):
            result = extractor.extract(b"fake pdf bytes")

        self.assertEqual(engine.calls, 2)  # one OCR call per rendered page
        self.assertEqual(result.text, "ocr text\nocr text")
        self.assertEqual(result.metadata["method"], "ocr")
        self.assertTrue(result.metadata["ocr_available"])
        self.assertEqual(result.metadata["ocr_engine"], "fake")
        fake_pdf.close.assert_called_once()

    def test_pdf_supports_by_extension_or_mimetype(self) -> None:
        extractor = PdfExtractor()
        self.assertTrue(extractor.supports("application/pdf", ""))
        self.assertTrue(extractor.supports(None, ".pdf"))
        self.assertFalse(extractor.supports("text/plain", ".txt"))


class ImageExtractorTests(unittest.TestCase):
    def test_supports_common_image_mimetypes_and_extensions(self) -> None:
        extractor = ImageExtractor(_FakeOcrEngine())
        self.assertTrue(extractor.supports("image/png", ""))
        self.assertTrue(extractor.supports("image/jpeg", ""))
        self.assertTrue(extractor.supports(None, ".png"))
        self.assertTrue(extractor.supports(None, ".jpg"))
        self.assertFalse(extractor.supports("text/plain", ".txt"))

    def test_extract_deskews_then_calls_ocr_engine(self) -> None:
        im = Image.new("RGB", (50, 20), "white")
        buf = io.BytesIO()
        im.save(buf, format="PNG")

        engine = _FakeOcrEngine(text="image text")
        extractor = ImageExtractor(engine)

        result = extractor.extract(buf.getvalue())

        self.assertEqual(result.text, "image text")
        self.assertEqual(engine.calls, 1)
        self.assertEqual(result.metadata["preprocessing"], "deskew")
        self.assertEqual(result.metadata["ocr_engine"], "fake")


if __name__ == "__main__":
    unittest.main()

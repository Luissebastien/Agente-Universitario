import io
import unittest
import zipfile

from extraction.ocr import (
    OcrEngine,
    OcrResult,
    deskew,
    estimate_skew_angle,
    extract_ooxml_slide_text,
    ocr_embedded_images,
)


def _make_text_image(text: str = "Hola mundo", angle: int = 0):
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (300, 80), "white")
    d = ImageDraw.Draw(im)
    d.text((10, 30), text, fill="black")
    if angle:
        im = im.rotate(angle, expand=True, fillcolor=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class _FakeOcrEngine(OcrEngine):
    name = "fake"
    version = "1"

    def __init__(self, text: str = "fake text", raise_on: set[str] | None = None):
        self._text = text
        self._raise_on = raise_on or set()

    def ocr(self, image_bytes: bytes) -> OcrResult:
        if b"BOOM" in image_bytes:
            raise ValueError("simulated OCR failure")
        return OcrResult(text=self._text, metadata={"engine": self.name})


class DeskewTests(unittest.TestCase):
    def test_straight_image_is_returned_unchanged(self) -> None:
        data = _make_text_image(angle=0)
        self.assertEqual(deskew(data), data)

    def test_rotated_image_estimate_matches_applied_rotation(self) -> None:
        from PIL import Image

        data = _make_text_image(angle=-12)
        im = Image.open(io.BytesIO(data))
        angle = estimate_skew_angle(im)
        # Correcting a -12deg rotation requires roughly +12deg.
        self.assertAlmostEqual(angle, 12.0, delta=2.0)

    def test_deskew_does_not_raise_on_undecodable_bytes(self) -> None:
        garbage = b"not an image"
        self.assertEqual(deskew(garbage), garbage)

    def test_deskew_returns_valid_image_bytes_when_correcting(self) -> None:
        from PIL import Image

        data = _make_text_image(angle=-12)
        result = deskew(data)
        # Must still be a decodable image after correction.
        Image.open(io.BytesIO(result)).verify()


class OcrEmbeddedImagesTests(unittest.TestCase):
    def _make_docx_like_zip(self, media_files: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, content in media_files.items():
                z.writestr(f"word/media/{name}", content)
        return buf.getvalue()

    def test_ocrs_each_embedded_image(self) -> None:
        data = self._make_docx_like_zip({"image1.png": b"fake-png-1", "image2.png": b"fake-png-2"})
        engine = _FakeOcrEngine(text="recognized text")

        texts, meta = ocr_embedded_images(data, "word/media/", engine)

        self.assertEqual(len(texts), 2)
        self.assertTrue(all("recognized text" in t for t in texts))
        self.assertEqual(len(meta), 2)
        self.assertTrue(all(m["status"] == "done" for m in meta))

    def test_no_media_yields_empty_result(self) -> None:
        data = self._make_docx_like_zip({})
        texts, meta = ocr_embedded_images(data, "word/media/", _FakeOcrEngine())
        self.assertEqual(texts, [])
        self.assertEqual(meta, [])

    def test_one_bad_image_does_not_abort_the_rest(self) -> None:
        data = self._make_docx_like_zip({"good.png": b"ok", "bad.png": b"BOOM-trigger"})
        texts, meta = ocr_embedded_images(data, "word/media/", _FakeOcrEngine())

        statuses = {m["file"]: m["status"] for m in meta if "file" in m}
        self.assertEqual(statuses["word/media/good.png"], "done")
        self.assertEqual(statuses["word/media/bad.png"], "failed")
        self.assertEqual(len(texts), 1)  # only the good image contributed text

    def test_respects_max_images_cap(self) -> None:
        media = {f"img{i}.png": b"x" for i in range(30)}
        data = self._make_docx_like_zip(media)

        texts, meta = ocr_embedded_images(data, "word/media/", _FakeOcrEngine(), max_images=5)

        done_entries = [m for m in meta if m.get("status") == "done"]
        self.assertEqual(len(done_entries), 5)
        truncation_entries = [m for m in meta if m.get("truncated")]
        self.assertEqual(len(truncation_entries), 1)
        self.assertEqual(truncation_entries[0]["total_embedded_images"], 30)

    def test_not_a_zip_returns_empty_without_raising(self) -> None:
        texts, meta = ocr_embedded_images(b"not a zip file", "word/media/", _FakeOcrEngine())
        self.assertEqual(texts, [])
        self.assertEqual(meta, [])


class ExtractOoxmlSlideTextTests(unittest.TestCase):
    def _make_presentation_zip(self, slide_texts: list[str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i, text in enumerate(slide_texts, start=1):
                xml = (
                    '<?xml version="1.0"?>'
                    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    f'<a:t>{text}</a:t></p:sld>'
                )
                z.writestr(f"ppt/slides/slide{i}.xml", xml)
        return buf.getvalue()

    def test_extracts_text_from_each_slide_in_order(self) -> None:
        data = self._make_presentation_zip(["First slide", "Second slide", "Third slide"])

        text, n_slides = extract_ooxml_slide_text(data)

        self.assertEqual(n_slides, 3)
        self.assertEqual(text, "First slide\nSecond slide\nThird slide")

    def test_works_regardless_of_declared_content_type(self) -> None:
        # This is exactly the .ppsx case: same slide XML structure, different
        # (and, for python-pptx, rejected) declared content-type - this
        # reader never looks at the content-type at all.
        data = self._make_presentation_zip(["Solo slide"])
        text, n_slides = extract_ooxml_slide_text(data)
        self.assertEqual(text, "Solo slide")
        self.assertEqual(n_slides, 1)

    def test_malformed_slide_xml_is_skipped_not_fatal(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ppt/slides/slide1.xml", "<not valid xml")
            z.writestr(
                "ppt/slides/slide2.xml",
                '<?xml version="1.0"?><p:sld xmlns:p="x" xmlns:a="y"><a:t>OK slide</a:t></p:sld>',
            )
        text, n_slides = extract_ooxml_slide_text(buf.getvalue())
        self.assertEqual(text, "OK slide")
        self.assertEqual(n_slides, 2)  # counted, even though one failed to parse


if __name__ == "__main__":
    unittest.main()

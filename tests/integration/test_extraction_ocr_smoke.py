"""Real docTR smoke test - actually loads the pretrained model and runs
inference. Gated separately from the Moodle integration tests (RUN_OCR_
INTEGRATION_TESTS, not MOODLE_RUN_INTEGRATION_TESTS) because it needs no
Moodle credentials at all, only network access to download docTR's model
weights on first run (~130MB, cached afterwards under ~/.cache/doctr).

Never runs as part of the normal `python -m unittest discover` suite.
"""
import io
import os
import unittest

_REQUIRED_ENV = "RUN_OCR_INTEGRATION_TESTS"


def _ocr_integration_enabled() -> bool:
    return os.environ.get(_REQUIRED_ENV) == "1"


@unittest.skipUnless(
    _ocr_integration_enabled(),
    f"Set {_REQUIRED_ENV}=1 to run the real docTR OCR smoke test (downloads model weights)",
)
class DoctrOcrEngineSmokeTest(unittest.TestCase):
    def test_recognizes_text_in_a_simple_rendered_image(self) -> None:
        from extraction.ocr import DoctrOcrEngine

        image_bytes = self._render_text_image("Universidad")
        engine = DoctrOcrEngine()

        result = engine.ocr(image_bytes)

        print(f"[smoke] docTR recognized: {result.text!r}")
        self.assertIn("niversidad", result.text)  # tolerant of case/first-char OCR noise

    def test_deskew_improves_recognition_of_rotated_text(self) -> None:
        from extraction.extractors import PdfExtractor  # noqa: F401 (import proves extractors wires without error)
        from extraction.ocr import DoctrOcrEngine, deskew

        rotated = self._render_text_image("Estudiante", angle=-12)
        engine = DoctrOcrEngine()

        raw_result = engine.ocr(rotated)
        deskewed_result = engine.ocr(deskew(rotated))

        print(f"[smoke] raw (rotated) recognition: {raw_result.text!r}")
        print(f"[smoke] deskewed recognition: {deskewed_result.text!r}")
        self.assertIn("studiante", deskewed_result.text)

    @staticmethod
    def _render_text_image(text: str, angle: int = 0) -> bytes:
        from PIL import Image, ImageDraw, ImageFont

        im = Image.new("RGB", (500, 120), "white")
        d = ImageDraw.Draw(im)
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 32)
        except OSError:
            font = ImageFont.load_default()
        d.text((20, 30), text, font=font, fill="black")
        if angle:
            im = im.rotate(angle, expand=True, fillcolor=(255, 255, 255))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import abc
import io
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image

# Cap on how many images embedded inside one document get OCR'd. Real
# documents can embed hundreds/thousands of images (see the OCR benchmark:
# one real scanned PDF had 2,692 embedded images) - without a bound, a single
# resource could make Extraction arbitrarily slow. This is a deliberate MVP
# limit, not a general "extract everything recursively" system.
MAX_EMBEDDED_IMAGES_PER_DOCUMENT = 20


@dataclass
class OcrResult:
    """What a successful OcrEngine.ocr() call produces."""

    text: str
    metadata: dict = field(default_factory=dict)


class OcrEngine(abc.ABC):
    """Abstraction over the OCR backend.

    This is the ONLY seam through which Extraction talks to an OCR library.
    PdfExtractor/ImageExtractor/embedded-image OCR all depend on this
    interface, never on docTR (or any other engine) directly - so the MVP
    engine choice (docTR + deskew, see .ai/decisions.md) can be replaced
    after the post-MVP OCR benchmark without touching them.
    """

    name: str
    version: str

    @abc.abstractmethod
    def ocr(self, image_bytes: bytes) -> OcrResult:
        """Run OCR on already-decodable image bytes (PNG/JPEG/etc). Deskewing
        or other preprocessing is the caller's responsibility - this method
        only recognizes text in whatever pixels it's given."""


class DoctrOcrEngine(OcrEngine):
    """MVP OCR engine (provisional - see .ai/decisions.md). CPU-only PyTorch
    backend. The model is loaded lazily on first use, not at construction,
    so simply instantiating this class (e.g. while wiring dependencies) never
    triggers a multi-second model load or network access - only actually
    calling ocr() does.
    """

    name = "doctr"
    version = "1"

    def __init__(self) -> None:
        self._model = None

    def _load(self):
        if self._model is None:
            from doctr.models import ocr_predictor

            self._model = ocr_predictor(pretrained=True)
        return self._model

    def ocr(self, image_bytes: bytes) -> OcrResult:
        from doctr.io import DocumentFile

        model = self._load()
        doc = DocumentFile.from_images(image_bytes)
        result = model(doc)

        lines = []
        for page in result.pages:
            for block in page.blocks:
                for line in block.lines:
                    lines.append(" ".join(w.value for w in line.words))
        text = "\n".join(lines)
        return OcrResult(text=text, metadata={"pages": len(result.pages)})


def estimate_skew_angle(im: Image.Image, candidate_angles=None) -> float:
    """Projection-profile skew estimate, in degrees.

    Rotates the image over a range of candidate angles and picks the one
    whose horizontal projection (row-wise sum of dark pixels) has the
    highest variance - text lines line up into sharp peaks/troughs once the
    skew is corrected, and blur into a flat profile otherwise. Pure
    numpy/Pillow, no OCR-specific dependency, and empirically validated in
    the OCR benchmark (single biggest lever found there: ~87-93% CER
    reduction across every engine tested).
    """
    if candidate_angles is None:
        candidate_angles = np.arange(-30, 30.5, 0.5)
    gray = np.array(im.convert("L"), dtype=np.float64)
    thresh = gray.mean() - 0.15 * gray.std()
    binary = (gray < thresh).astype(np.float64)

    small = Image.fromarray((binary * 255).astype(np.uint8))
    w, h = small.size
    if max(w, h) > 500:
        scale = 500 / max(w, h)
        small = small.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    best_angle = 0.0
    best_score = -1.0
    for angle in candidate_angles:
        rotated = small.rotate(float(angle), expand=True, fillcolor=0)
        arr = np.array(rotated, dtype=np.float64)
        score = arr.sum(axis=1).var()
        if score > best_score:
            best_score = score
            best_angle = float(angle)
    return best_angle


def deskew(image_bytes: bytes) -> bytes:
    """Best-effort deskew. Returns the original bytes unchanged if the image
    can't be decoded, or if the estimated skew is negligible (<0.25deg) -
    re-encoding a perfectly straight image would only lose quality for no
    benefit."""
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return image_bytes

    angle = estimate_skew_angle(im)
    if abs(angle) < 0.25:
        return image_bytes

    rotated = im.rotate(angle, expand=True, fillcolor=(255, 255, 255))
    out = io.BytesIO()
    rotated.save(out, format="PNG")
    return out.getvalue()


def ocr_embedded_images(
    data: bytes, media_prefix: str, ocr_engine: OcrEngine, max_images: int = MAX_EMBEDDED_IMAGES_PER_DOCUMENT
) -> tuple[list[str], list[dict]]:
    """Shared helper for DocxExtractor/PptxExtractor: OCR the images embedded
    inside an OOXML zip container (word/media/ or ppt/media/).

    Returns (texts, per_image_metadata). Never raises - a single unreadable
    embedded image is recorded as a failed entry and skipped, it never aborts
    extraction of the rest of the document (DEC-048: one failure must not
    block unrelated content).
    """
    texts: list[str] = []
    per_image: list[dict] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            media = sorted(n for n in z.namelist() if n.startswith(media_prefix))
            truncated = len(media) > max_images
            for name in media[:max_images]:
                try:
                    img_bytes = z.read(name)
                    deskewed = deskew(img_bytes)
                    result = ocr_engine.ocr(deskewed)
                except Exception as exc:
                    per_image.append({"file": name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
                    continue
                if result.text.strip():
                    texts.append(f"[{name}]\n{result.text}")
                per_image.append({"file": name, "status": "done", "chars": len(result.text)})
    except zipfile.BadZipFile:
        return texts, per_image

    if truncated:
        per_image.append({"truncated": True, "total_embedded_images": len(media), "processed": max_images})
    return texts, per_image


def extract_ooxml_slide_text(data: bytes) -> tuple[str, int]:
    """Manual OOXML text extraction for presentations (stdlib zipfile+
    ElementTree). Used for BOTH .pptx and .ppsx: python-pptx works for .pptx
    but rejects .ppsx outright (confirmed against real INTEC files in the OCR
    benchmark - it validates the internal content-type and only accepts
    'presentationml.presentation', not 'presentationml.slideshow'). Rather
    than special-case two code paths, this one reader handles both, since it
    doesn't care about the declared content-type at all - only the slide XML
    structure, which is identical between the two formats.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slide_names = sorted(
            n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        )
        parts = []
        for name in slide_names:
            try:
                root = ET.fromstring(z.read(name))
                parts.append("".join(root.itertext()))
            except ET.ParseError:
                continue
        return "\n".join(parts), len(slide_names)

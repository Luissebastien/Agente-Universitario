from __future__ import annotations

import abc
import io
import logging
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# Cap on how many images embedded inside one document get OCR'd. Real
# documents can embed hundreds/thousands of images (see the OCR benchmark:
# one real scanned PDF had 2,692 embedded images) - without a bound, a single
# resource could make Extraction arbitrarily slow. This is a deliberate MVP
# limit, not a general "extract everything recursively" system.
MAX_EMBEDDED_IMAGES_PER_DOCUMENT = 20

# Ceiling on the pixels of an image we will decode. A full page scanned at
# 600 dpi is ~34.8 MP (A4) / ~33.7 MP (Letter), so 50 MP covers the heaviest
# realistic document scan with room to spare; above that it is not a page.
# Set here explicitly rather than relying on Pillow's own default, which only
# *warns* up to 89.5 MP and errors above 179 MP - and which is process-global
# state this module should not mutate.
MAX_IMAGE_PIXELS = 50_000_000

# Longest side the skew search works on. Reducing to this size before any
# array is allocated is what keeps peak memory independent of input size.
_SKEW_WORKING_PIXELS = 500

# Office formats (DOCX/PPTX/XLSX/ODT/ODS) are zip containers, so extracting
# any of them means decompressing zip members. These bound one member, the
# running total per document, and how far a member may expand - all three are
# read from the zip's central directory, without decompressing anything.
# (A .zip or .rar as course material is a different thing entirely and is
# not a supported format at all - it is recorded as 'unsupported'.)
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 100


class ImageTooLargeError(Exception):
    """An image declares more pixels than MAX_IMAGE_PIXELS, so it is not decoded.

    Raised rather than silently returning the original bytes: an image we
    refuse to look at must become an explicit failed extraction attempt
    (DEC-048), not a silently empty success.
    """


def _unsafe_zip_member(info: zipfile.ZipInfo, running_total: int) -> str | None:
    """Why this member must not be read, or None when it may be."""
    if info.file_size > MAX_ZIP_MEMBER_BYTES:
        return f"{info.file_size} bytes uncompressed, over the {MAX_ZIP_MEMBER_BYTES}-byte limit"
    if info.compress_size > 0:
        ratio = info.file_size / info.compress_size
        if ratio > MAX_ZIP_COMPRESSION_RATIO:
            return f"compression ratio {ratio:.0f}:1, over {MAX_ZIP_COMPRESSION_RATIO}:1"
    if running_total + info.file_size > MAX_ZIP_TOTAL_BYTES:
        return f"would pass the {MAX_ZIP_TOTAL_BYTES}-byte total for one document"
    return None


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

    The reduction to _SKEW_WORKING_PIXELS happens BEFORE any array is
    allocated, because the search only ever needs the reduced copy. That
    ordering makes peak memory independent of the input: measured at a
    constant ~5.7 MB, instead of ~25 bytes per input pixel (~2.2 GB for an
    image Pillow accepts with only a warning).
    """
    if candidate_angles is None:
        candidate_angles = np.arange(-30, 30.5, 0.5)

    w, h = im.size
    if max(w, h) > _SKEW_WORKING_PIXELS:
        scale = _SKEW_WORKING_PIXELS / max(w, h)
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    gray = np.asarray(im.convert("L"), dtype=np.float32)
    thresh = gray.mean() - 0.15 * gray.std()
    small = Image.fromarray(((gray < thresh) * 255).astype(np.uint8))

    best_angle = 0.0
    best_score = -1.0
    for angle in candidate_angles:
        rotated = small.rotate(float(angle), expand=True, fillcolor=0)
        score = np.asarray(rotated, dtype=np.float32).sum(axis=1).var()
        if score > best_score:
            best_score = score
            best_angle = float(angle)
    return best_angle


def deskew(image_bytes: bytes) -> bytes:
    """Best-effort deskew. Returns the original bytes unchanged if the image
    can't be decoded, or if the estimated skew is negligible (<0.25deg) -
    re-encoding a perfectly straight image would only lose quality for no
    benefit.

    Raises ImageTooLargeError for an image over MAX_IMAGE_PIXELS. The size
    is read from the header before any pixel is decoded, and the error is
    deliberately not swallowed: returning the original bytes would hand the
    oversized image straight to the OCR engine, which has no such limit.
    """
    try:
        im = Image.open(io.BytesIO(image_bytes))
        width, height = im.size  # header only - nothing decoded yet
    except Image.DecompressionBombError as exc:
        raise ImageTooLargeError(str(exc)) from None
    except Exception:
        return image_bytes

    if width * height > MAX_IMAGE_PIXELS:
        raise ImageTooLargeError(
            f"{width}x{height} = {width * height} pixels, over the "
            f"{MAX_IMAGE_PIXELS}-pixel limit"
        )

    try:
        im = im.convert("RGB")
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
            media = sorted(
                (i for i in z.infolist() if i.filename.startswith(media_prefix)),
                key=lambda i: i.filename,
            )
            truncated = len(media) > max_images
            total_bytes = 0
            for info in media[:max_images]:
                name = info.filename
                unsafe = _unsafe_zip_member(info, total_bytes)
                if unsafe is not None:
                    logger.warning("Skipped embedded image %s: %s", name, unsafe)
                    per_image.append({"file": name, "status": "skipped", "reason": unsafe})
                    continue
                total_bytes += info.file_size
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
    but rejects .ppsx outright (confirmed against real la instancia real files in the OCR
    benchmark - it validates the internal content-type and only accepts
    'presentationml.presentation', not 'presentationml.slideshow'). Rather
    than special-case two code paths, this one reader handles both, since it
    doesn't care about the declared content-type at all - only the slide XML
    structure, which is identical between the two formats.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted(
            (
                i for i in z.infolist()
                if i.filename.startswith("ppt/slides/slide") and i.filename.endswith(".xml")
            ),
            key=lambda i: i.filename,
        )
        parts = []
        total_bytes = 0
        for info in slides:
            unsafe = _unsafe_zip_member(info, total_bytes)
            if unsafe is not None:
                logger.warning("Skipped slide %s: %s", info.filename, unsafe)
                continue
            total_bytes += info.file_size
            try:
                root = ET.fromstring(z.read(info.filename))
                parts.append("".join(root.itertext()))
            except ET.ParseError:
                continue
        return "\n".join(parts), len(slides)

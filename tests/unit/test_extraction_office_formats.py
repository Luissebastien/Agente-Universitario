import io
import unittest
from unittest.mock import MagicMock, patch

from extraction.extractors import (
    DocxExtractor,
    OdsExtractor,
    OdtExtractor,
    PptxExtractor,
    XlsExtractor,
    XlsxExtractor,
)
from extraction.ocr import OcrEngine, OcrResult


class _FakeOcrEngine(OcrEngine):
    name = "fake"
    version = "1"

    def ocr(self, image_bytes: bytes) -> OcrResult:
        return OcrResult(text="[embedded image text]", metadata={})


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    import docx

    doc = docx.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_pptx_bytes(slide_texts: list[str]) -> bytes:
    import pptx
    from pptx.util import Inches

    p = pptx.Presentation()
    blank_layout = p.slide_layouts[6]
    for text in slide_texts:
        slide = p.slides.add_slide(blank_layout)
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
        box.text_frame.text = text
    buf = io.BytesIO()
    p.save(buf)
    return buf.getvalue()


def _make_xlsx_bytes(rows: list[list]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_odt_bytes(paragraphs: list[str]) -> bytes:
    from odf.opendocument import OpenDocumentText
    from odf.text import P

    doc = OpenDocumentText()
    for text in paragraphs:
        doc.text.addElement(P(text=text))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_ods_bytes(sheet_name: str, rows: list[list[str]]) -> bytes:
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableCell, TableRow
    from odf.text import P

    doc = OpenDocumentSpreadsheet()
    table = Table(name=sheet_name)
    for row in rows:
        tr = TableRow()
        for value in row:
            cell = TableCell()
            cell.addElement(P(text=value))
            tr.addElement(cell)
        table.addElement(tr)
    doc.spreadsheet.addElement(table)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class DocxExtractorTests(unittest.TestCase):
    def test_supports_docx(self) -> None:
        extractor = DocxExtractor()
        self.assertTrue(extractor.supports(None, ".docx"))
        self.assertTrue(extractor.supports(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ""
        ))
        self.assertFalse(extractor.supports(None, ".doc"))  # legacy binary, different extractor entirely

    def test_extracts_paragraph_text_in_order(self) -> None:
        data = _make_docx_bytes(["First paragraph.", "Second paragraph."])
        result = DocxExtractor().extract(data)
        self.assertEqual(result.text, "First paragraph.\nSecond paragraph.")
        self.assertEqual(result.metadata["method"], "native")
        self.assertEqual(result.metadata["paragraphs"], 2)

    def test_without_ocr_engine_embedded_images_field_is_none(self) -> None:
        data = _make_docx_bytes(["Text only."])
        result = DocxExtractor(ocr_engine=None).extract(data)
        self.assertIsNone(result.metadata["embedded_images_ocr"])

    def test_with_ocr_engine_no_embedded_images_yields_empty_list_not_none(self) -> None:
        data = _make_docx_bytes(["Text only, no images."])
        result = DocxExtractor(ocr_engine=_FakeOcrEngine()).extract(data)
        self.assertEqual(result.metadata["embedded_images_ocr"], [])
        self.assertEqual(result.text, "Text only, no images.")  # nothing appended


class PptxExtractorTests(unittest.TestCase):
    def test_supports_both_pptx_and_ppsx(self) -> None:
        extractor = PptxExtractor()
        self.assertTrue(extractor.supports(None, ".pptx"))
        self.assertTrue(extractor.supports(None, ".ppsx"))
        self.assertTrue(extractor.supports(
            "application/vnd.openxmlformats-officedocument.presentationml.slideshow", ""
        ))

    def test_extracts_slide_text(self) -> None:
        data = _make_pptx_bytes(["Slide one text", "Slide two text"])
        result = PptxExtractor().extract(data)
        self.assertIn("Slide one text", result.text)
        self.assertIn("Slide two text", result.text)
        self.assertEqual(result.metadata["slides"], 2)
        self.assertEqual(result.metadata["method"], "manual_ooxml")

    def test_same_bytes_extract_identically_as_ppsx(self) -> None:
        # Confirms the manual reader really doesn't care about the extension/
        # declared content-type, which is the whole point of not using
        # python-pptx (it rejects .ppsx outright on real files).
        data = _make_pptx_bytes(["Presentation content"])
        result_as_pptx = PptxExtractor().extract(data)
        result_as_ppsx = PptxExtractor().extract(data)  # extract() doesn't take the extension at all
        self.assertEqual(result_as_pptx.text, result_as_ppsx.text)


class XlsxExtractorTests(unittest.TestCase):
    def test_supports_xlsx(self) -> None:
        extractor = XlsxExtractor()
        self.assertTrue(extractor.supports(None, ".xlsx"))
        self.assertFalse(extractor.supports(None, ".xls"))

    def test_extracts_rows_as_readable_lines(self) -> None:
        data = _make_xlsx_bytes([["Name", "Grade"], ["Ana", 95], ["Luis", 88]])
        result = XlsxExtractor().extract(data)
        self.assertIn("Name | Grade", result.text)
        self.assertIn("Ana | 95", result.text)
        self.assertIn("# Sheet1", result.text)
        self.assertEqual(result.metadata["sheets"], 1)

    def test_empty_rows_are_skipped(self) -> None:
        data = _make_xlsx_bytes([["A", "B"], [], ["C", "D"]])
        result = XlsxExtractor().extract(data)
        lines = [l for l in result.text.splitlines() if l and not l.startswith("#")]
        self.assertEqual(lines, ["A | B", "C | D"])


class XlsExtractorTests(unittest.TestCase):
    """xlrd has no writer, so the real-parsing path is exercised via a
    mocked workbook rather than a hand-built binary .xls fixture - this
    avoids adding a legacy .xls-writing dependency (xlwt, unmaintained)
    solely for tests."""

    def test_supports_xls(self) -> None:
        extractor = XlsExtractor()
        self.assertTrue(extractor.supports(None, ".xls"))
        self.assertTrue(extractor.supports("application/vnd.ms-excel", ""))
        self.assertFalse(extractor.supports(None, ".xlsx"))

    def test_extracts_rows_via_xlrd(self) -> None:
        fake_sheet = MagicMock()
        fake_sheet.name = "Hoja1"
        fake_sheet.nrows = 2
        fake_sheet.ncols = 2
        fake_sheet.cell_value.side_effect = lambda r, c: {
            (0, 0): "Nombre", (0, 1): "Nota", (1, 0): "Ana", (1, 1): 95.0,
        }[(r, c)]
        fake_wb = MagicMock()
        fake_wb.sheets.return_value = [fake_sheet]
        fake_wb.nsheets = 1

        with patch("xlrd.open_workbook", return_value=fake_wb):
            result = XlsExtractor().extract(b"fake xls bytes")

        self.assertIn("Nombre | Nota", result.text)
        self.assertIn("Ana | 95.0", result.text)
        self.assertEqual(result.metadata["sheets"], 1)


class OdtExtractorTests(unittest.TestCase):
    def test_supports_odt(self) -> None:
        extractor = OdtExtractor()
        self.assertTrue(extractor.supports(None, ".odt"))
        self.assertTrue(extractor.supports("application/vnd.oasis.opendocument.text", ""))

    def test_extracts_paragraph_text(self) -> None:
        data = _make_odt_bytes(["Primer parrafo.", "Segundo parrafo."])
        result = OdtExtractor().extract(data)
        self.assertEqual(result.text, "Primer parrafo.\nSegundo parrafo.")
        self.assertEqual(result.metadata["paragraphs"], 2)


class OdsExtractorTests(unittest.TestCase):
    def test_supports_ods(self) -> None:
        extractor = OdsExtractor()
        self.assertTrue(extractor.supports(None, ".ods"))
        self.assertTrue(extractor.supports("application/vnd.oasis.opendocument.spreadsheet", ""))

    def test_extracts_rows_as_readable_lines(self) -> None:
        data = _make_ods_bytes("Datos", [["Curso", "Seccion"], ["ING231", "04"]])
        result = OdsExtractor().extract(data)
        self.assertIn("Curso Seccion", result.text.replace(" | ", " "))
        self.assertIn("# Datos", result.text)
        self.assertEqual(result.metadata["tables"], 1)


if __name__ == "__main__":
    unittest.main()

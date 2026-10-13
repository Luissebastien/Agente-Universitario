import unittest

from extraction.extractors import HtmlExtractor, PlainTextExtractor


class PlainTextExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = PlainTextExtractor()

    def test_supports_text_plain_mimetype(self) -> None:
        self.assertTrue(self.extractor.supports("text/plain", ""))

    def test_supports_uri_list_mimetype(self) -> None:
        self.assertTrue(self.extractor.supports("text/uri-list", ""))

    def test_supports_txt_extension_fallback(self) -> None:
        self.assertTrue(self.extractor.supports(None, ".txt"))

    def test_does_not_support_html(self) -> None:
        self.assertFalse(self.extractor.supports("text/html", ".html"))

    def test_extract_decodes_utf8_text(self) -> None:
        result = self.extractor.extract("hola mundo".encode("utf-8"))
        self.assertEqual(result.text, "hola mundo")
        self.assertEqual(result.metadata["char_count"], len("hola mundo"))

    def test_extract_raises_on_undecodable_bytes(self) -> None:
        with self.assertRaises(UnicodeDecodeError):
            self.extractor.extract(b"\xff\xfe\x00\x01")


class HtmlExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = HtmlExtractor()

    def test_supports_text_html_mimetype(self) -> None:
        self.assertTrue(self.extractor.supports("text/html", ""))

    def test_supports_html_extension_fallback(self) -> None:
        self.assertTrue(self.extractor.supports(None, ".html"))
        self.assertTrue(self.extractor.supports(None, ".htm"))

    def test_does_not_support_plain_text(self) -> None:
        self.assertFalse(self.extractor.supports("text/plain", ".txt"))

    def test_extract_strips_tags(self) -> None:
        html = b"<html><body><h1>Welcome</h1><p>Hello there.</p></body></html>"
        result = self.extractor.extract(html)
        self.assertEqual(result.text, "Welcome\nHello there.")

    def test_extract_excludes_script_and_style_content(self) -> None:
        html = (
            b"<html><head><style>body{color:red}</style></head>"
            b"<body><script>alert('x')</script><p>Visible</p></body></html>"
        )
        result = self.extractor.extract(html)
        self.assertEqual(result.text, "Visible")

    def test_extract_metadata_includes_tag_count(self) -> None:
        html = b"<div><p>a</p><p>b</p></div>"
        result = self.extractor.extract(html)
        self.assertEqual(result.metadata["tag_count"], 3)  # div, p, p

    def test_extract_raises_on_undecodable_bytes(self) -> None:
        with self.assertRaises(UnicodeDecodeError):
            self.extractor.extract(b"\xff\xfe\x00\x01")

    # -- alt text ---------------------------------------------------------
    # A Moodle page written in the editor is often nothing but images, and
    # then alt carries the only words a person wrote. These reproduce the
    # real resources that extracted to nothing in production.

    def test_image_alt_text_is_extracted(self) -> None:
        html = (
            b'<div class="no-overflow"><p><strong><img class="img-fluid" '
            b'src="GDS_QueHaremosSemana.png" alt="Guia semanal de Actividades" '
            b'width="99%"></strong></p></div>'
        )
        result = self.extractor.extract(html)

        self.assertEqual(result.text, "Guia semanal de Actividades")
        self.assertEqual(result.metadata["attribute_chunks"], 1)

    def test_alt_text_is_combined_with_the_surrounding_prose_in_order(self) -> None:
        html = b'<p>Antes</p><img src="x.png" alt="La figura"><p>Despues</p>'
        result = self.extractor.extract(html)

        self.assertEqual(result.text, "Antes\nLa figura\nDespues")

    def test_decorative_alt_adds_nothing(self) -> None:
        html = b'<p><img src="linea.png" alt=""><img src="x.png" alt="   "></p>'
        result = self.extractor.extract(html)

        self.assertEqual(result.text, "")
        self.assertEqual(result.metadata["attribute_chunks"], 0)

    def test_only_alt_is_read_not_other_attributes(self) -> None:
        # title on the real corpus was boilerplate ("YouTube video player"),
        # so a page that is only an embed still extracts to nothing.
        html = (
            b'<div class="no-overflow"><p><iframe title="YouTube video player" '
            b'src="https://www.youtube.com/embed/aLJMEfv1JME" width="560"></iframe></p></div>'
        )
        result = self.extractor.extract(html)

        self.assertEqual(result.text, "")

    def test_alt_inside_skipped_markup_is_ignored(self) -> None:
        html = b'<script><img src="x.png" alt="no debe salir"></script><p>Si</p>'
        result = self.extractor.extract(html)

        self.assertEqual(result.text, "Si")

    def test_prose_pages_are_unaffected(self) -> None:
        html = b"<html><body><h1>Tema 1</h1><p>Contenido real.</p></body></html>"
        result = self.extractor.extract(html)

        self.assertEqual(result.text, "Tema 1\nContenido real.")
        self.assertEqual(result.metadata["attribute_chunks"], 0)


if __name__ == "__main__":
    unittest.main()

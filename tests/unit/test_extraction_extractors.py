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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from html.parser import HTMLParser

_PLAIN_TEXT_MIMETYPES = {"text/plain", "text/uri-list"}
_PLAIN_TEXT_EXTENSIONS = {".txt"}

_HTML_MIMETYPES = {"text/html"}
_HTML_EXTENSIONS = {".html", ".htm"}

# Elements whose contents are not visible document text.
_SKIP_TAGS = {"script", "style"}


@dataclass
class ExtractionResult:
    """What a successful Extractor.extract() call produces."""

    text: str
    metadata: dict = field(default_factory=dict)


class Extractor(abc.ABC):
    """A deterministic converter from raw bytes of a known format to text.

    No LLM, no interpretation of meaning - purely mechanical decoding/parsing.
    Selection is by declared mimetype with a filename-extension fallback,
    mirroring the same "extension + Moodle-declared MIME" classification
    already used elsewhere in this project rather than sniffing content.
    """

    name: str
    version: str

    @abc.abstractmethod
    def supports(self, mimetype: str | None, extension: str) -> bool: ...

    @abc.abstractmethod
    def extract(self, data: bytes) -> ExtractionResult: ...


class PlainTextExtractor(Extractor):
    """Decodes UTF-8 plain text content, including preserved URL references."""

    name = "plain_text"
    version = "1"

    def supports(self, mimetype: str | None, extension: str) -> bool:
        return mimetype in _PLAIN_TEXT_MIMETYPES or extension in _PLAIN_TEXT_EXTENSIONS

    def extract(self, data: bytes) -> ExtractionResult:
        # Strict decode: undecodable bytes mean the content isn't actually
        # what it claimed to be, which should surface as an explicit failure
        # (DEC-048) rather than be silently mangled with errors="replace".
        text = data.decode("utf-8")
        return ExtractionResult(text=text, metadata={"char_count": len(text)})


class _VisibleTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0
        self.tag_count = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tag_count += 1
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self._chunks.append(stripped)

    def get_text(self) -> str:
        return "\n".join(self._chunks)


class HtmlExtractor(Extractor):
    """Strips markup to plain visible text using the stdlib HTML parser.

    Deliberately does not resolve/fetch anything referenced by the HTML
    (images, links, scripts) - that would be crawling, out of scope here.
    """

    name = "html_text"
    version = "1"

    def supports(self, mimetype: str | None, extension: str) -> bool:
        return mimetype in _HTML_MIMETYPES or extension in _HTML_EXTENSIONS

    def extract(self, data: bytes) -> ExtractionResult:
        raw = data.decode("utf-8")
        parser = _VisibleTextHTMLParser()
        parser.feed(raw)
        text = parser.get_text()
        return ExtractionResult(
            text=text,
            metadata={
                "char_count": len(text),
                "raw_char_count": len(raw),
                "tag_count": parser.tag_count,
            },
        )


# Ordered list of known extractors - first match wins. Adding a new format
# means adding a new Extractor here, not modifying Extraction's orchestration.
DEFAULT_EXTRACTORS: list[Extractor] = [PlainTextExtractor(), HtmlExtractor()]

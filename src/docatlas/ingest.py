"""Bounded extraction with provenance. No crawling, executable markup or OCR."""

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf", ".docx"}
EXTRACTION_PROFILE = "mixed-v2"
MAX_CHARACTERS = 2_000_000
MAX_CHUNKS = 4000


@dataclass
class Chunk:
    id: str
    text: str
    section: str
    page: int | None
    ordinal: int


class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract(filename: str, content: bytes, warnings=None) -> list[tuple[int | None, str]]:
    warnings = warnings if warnings is not None else []
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError("Supported files: Markdown, TXT, HTML, DOCX and text-based PDF.")
    if suffix == ".docx":
        warnings.append(
            "DOCX tables are flattened by row; verify merged cells, footnotes and figures in the original."
        )
        pages = [(None, extract_docx(content))]
    elif suffix == ".pdf":
        warnings.append(
            "PDF text order and tables may differ from the original layout; inspect the original for numerical claims."
        )
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted or len(reader.pages) > 300:
                raise ValueError("PDF must be unencrypted and no more than 300 pages.")
            pages = []
            total = 0
            for i, page in enumerate(reader.pages):
                value = page.extract_text() or ""
                total += len(value)
                if total > MAX_CHARACTERS:
                    raise ValueError("Extracted text is too large.")
                if len(value.strip()) < 30:
                    warnings.append(
                        f"Page {i + 1} has little or no extractable text; OCR or source inspection is needed."
                    )
                pages.append((i + 1, value))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Unable to extract this PDF.") from exc
    else:
        try:
            value = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Text files must use UTF-8 encoding.") from exc
        if suffix in {".html", ".htm"}:
            parser = VisibleHTML()
            parser.feed(value)
            value = "".join(parser.parts)
        pages = [(None, value)]
    if sum(len(text) for _, text in pages) > MAX_CHARACTERS:
        raise ValueError("Extracted text is too large.")
    if not any(text.strip() for _, text in pages):
        raise ValueError("No extractable text. Scanned PDFs require OCR before upload.")
    return pages


def extract_docx(content):
    """Bound ZIP expansion and parse paragraphs/tables without external entities."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(e.file_size for e in entries) > 20_000_000:
                raise ValueError("DOCX archive exceeds extraction limits.")
            if any(e.flag_bits & 1 for e in entries):
                raise ValueError("Encrypted DOCX files are unsupported.")
            info = archive.getinfo("word/document.xml")
            if info.file_size > 8_000_000:
                raise ValueError("DOCX document XML is too large.")
            data = archive.read(info)
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ValueError("DOCX XML entity declarations are unsupported.")
        root = ElementTree.fromstring(data)
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        body = root.find(namespace + "body")
        if body is None:
            raise ValueError("DOCX has no document body.")

        def text(element):
            return "".join(t.text or "" for t in element.iter(namespace + "t"))

        parts = []
        for element in body:
            if element.tag == namespace + "p":
                value = text(element)
                style = element.find(".//" + namespace + "pStyle")
                if style is not None and "heading" in style.get(namespace + "val", "").lower():
                    value = "# " + value
                parts.append(value)
            elif element.tag == namespace + "tbl":
                parts.append("[Table: cells separated by |; verify merged cells in the original]")
                for row in element.findall(namespace + "tr"):
                    parts.append(" | ".join(text(cell) for cell in row.findall(namespace + "tc")))
        return "\n".join(parts)
    except ValueError:
        raise
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, RuntimeError, OSError) as exc:
        raise ValueError("Unable to extract this DOCX. Use an unencrypted .docx file.") from exc


def chunk_document(document_id: str, pages, size: int = 1200, overlap: int = 180):
    if size < 100 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk size or overlap.")
    chunks = []
    for page, value in pages:
        value = value.replace("\x00", "").replace("\r\n", "\n")
        sections = re.split(r"(?m)(?=^#{1,6} )", value)
        for section_text in sections:
            section_text = section_text.strip()
            if not section_text:
                continue
            heading = re.match(r"^#{1,6}\s+(.+)", section_text)
            section = heading.group(1)[:200] if heading else "Document"
            start = 0
            while start < len(section_text):
                end = min(start + size, len(section_text))
                if end < len(section_text):
                    boundary = section_text.rfind("\n", start + size // 2, end)
                    if boundary != -1:
                        end = boundary
                text = section_text[start:end].strip()
                if text:
                    ordinal = len(chunks)
                    key = f"{document_id}:{page}:{ordinal}:{text}"
                    chunks.append(
                        Chunk(
                            hashlib.sha256(key.encode()).hexdigest()[:24],
                            text,
                            section,
                            page,
                            ordinal,
                        )
                    )
                if len(chunks) > MAX_CHUNKS:
                    raise ValueError("Document produces too many chunks.")
                if end == len(section_text):
                    break
                start = max(start + 1, end - overlap)
    return chunks

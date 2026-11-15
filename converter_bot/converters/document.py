"""Pure-Python document conversions.

This module deliberately avoids pandoc, LibreOffice, Calibre and Microsoft
Word so it can run inside a Vercel Function.

Engine map:
  * PDF / EPUB / FB2 / XPS reading, text extraction and PDF rendering -> PyMuPDF
  * DOCX reading -> python-docx; DOCX -> HTML/Markdown -> mammoth
  * Markdown -> HTML -> ``markdown``; HTML -> Markdown -> ``html2text``
  * DOCX/HTML/Markdown/Text -> PDF -> PyMuPDF ``Story`` (HTML/CSS renderer)
  * EPUB writing -> a tiny standards-compliant EPUB 3 writer below
"""

from __future__ import annotations

import asyncio
import html as html_lib
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import html2text
import mammoth
import markdown as markdown_lib
import pymupdf as fitz
import pymupdf4llm
from bs4 import BeautifulSoup
from docx import Document
from docx.shared import Pt

from config import cpu_executor

from . import ConversionError

SUPPORTED_HTML_SOURCES = {"pdf", "epub", "docx", "html", "md", "txt"}
BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre"]

_A4 = "a4"
_PAGE_MARGIN = 36
_MAX_PDF_PAGES = 5000


async def _run_cpu(function, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(cpu_executor, function, *args)


# ---------------------------------------------------------------------------
# Small text/HTML helpers
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _wrap_html(body: str, title: str = "Document") -> str:
    return (
        "<!DOCTYPE html><html><head>"
        f"<meta charset=\"utf-8\"><title>{html_lib.escape(title)}</title>"
        "<style>body{font-family:Calibri,Arial,sans-serif;font-size:11pt;"
        "line-height:1.45;margin:0;}h1,h2,h3,h4{line-height:1.2;}"
        "pre{white-space:pre-wrap;font-family:Consolas,monospace;}"
        "img{max-width:100%;}</style>"
        f"</head><body>{body}</body></html>"
    )


def _text_to_html(text: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    if not blocks:
        return "<p></p>"
    parts = []
    for block in blocks:
        escaped = "<br/>".join(html_lib.escape(line) for line in block.splitlines())
        parts.append(f"<p>{escaped}</p>")
    return "".join(parts)


def _markdown_to_html(text: str) -> str:
    return markdown_lib.markdown(
        text,
        extensions=["extra", "tables", "sane_lists", "fenced_code"],
    )


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _html_to_markdown(html: str) -> str:
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = False
    converter.ignore_links = False
    converter.unicode_snob = True
    return converter.handle(html).strip()


def _extract_body(html: str) -> str:
    match = re.search(r"<body[^>]*>(.*?)</body>", html, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else html


# ---------------------------------------------------------------------------
# Reading sources
# ---------------------------------------------------------------------------


def _fitz_pages_html_sync(path: str) -> str:
    document = fitz.open(path)
    try:
        parts = []
        for page in document:
            body = _extract_body(page.get_text("html"))
            if body.strip():
                parts.append(f'<div class="page">{body}</div>')
        return "\n".join(parts)
    finally:
        document.close()


def _fitz_extract_text_sync(path: str) -> str:
    document = fitz.open(path)
    try:
        return "\n\n".join(page.get_text() for page in document).strip()
    finally:
        document.close()


def _docx_to_html_fragment_sync(path: str) -> str:
    with open(path, "rb") as handle:
        result = mammoth.convert_to_html(handle)
    for message in result.messages:
        # Mammoth warnings are useful in logs but should not fail a conversion.
        if getattr(message, "type", "") == "error":
            raise ConversionError(f"Could not read this DOCX: {message.message}")
    return result.value


def _docx_to_markdown_sync(path: str) -> str:
    with open(path, "rb") as handle:
        result = mammoth.convert_to_markdown(handle)
    return result.value.strip()


def _docx_to_text_sync(path: str) -> str:
    document = Document(path)
    lines = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                lines.append("\t".join(cells))
    return "\n".join(lines).strip()


def _source_to_html_sync(source_format: str, path: str) -> str:
    source_format = source_format.lower()
    if source_format in ("pdf", "epub"):
        return _fitz_pages_html_sync(path)
    if source_format == "docx":
        return _docx_to_html_fragment_sync(path)
    if source_format == "html":
        # Keep only the body so callers can safely wrap it again.
        return _extract_body(_read_text(path))
    if source_format == "md":
        return _markdown_to_html(_read_text(path))
    if source_format == "txt":
        return _text_to_html(_read_text(path))
    raise ConversionError(f"Unsupported source format: .{source_format}")


# ---------------------------------------------------------------------------
# Writing formats
# ---------------------------------------------------------------------------


def _html_to_docx_sync(html: str, output_path: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body or soup
    document = Document()

    try:
        normal = document.styles["Normal"]
        normal.font.name = "Calibri"
        normal.font.size = Pt(11)
    except Exception:
        pass

    added = 0
    for element in body.find_all(BLOCK_TAGS):
        # Nested blocks are represented by their parent to avoid duplicates.
        if element.find_parent(BLOCK_TAGS):
            continue

        text = element.get_text(" ", strip=True)
        if not text:
            continue

        if element.name.startswith("h") and len(element.name) == 2:
            level = int(element.name[1])
            document.add_heading(text, level=min(level, 9))
        elif element.name == "li":
            style = "List Number" if element.find_parent("ol") else "List Bullet"
            try:
                document.add_paragraph(text, style=style)
            except Exception:
                document.add_paragraph(f"• {text}")
        elif element.name == "blockquote":
            paragraph = document.add_paragraph(text)
            try:
                paragraph.style = document.styles["Quote"]
            except Exception:
                pass
        elif element.name == "pre":
            paragraph = document.add_paragraph(text)
            try:
                paragraph.style = document.styles["No Spacing"]
            except Exception:
                pass
            for run in paragraph.runs:
                run.font.name = "Consolas"
                run.font.size = Pt(9)
        else:
            document.add_paragraph(text)
        added += 1

    if not added:
        text = body.get_text("\n", strip=True)
        if text:
            document.add_paragraph(text)

    document.save(output_path)


def _render_pdf(html: str, output_path: str) -> None:
    if "<html" not in html[:200].lower():
        html = _wrap_html(html)

    story = fitz.Story(html=html)
    writer = fitz.DocumentWriter(output_path)
    mediabox = fitz.paper_rect(_A4)
    where = mediabox + (_PAGE_MARGIN, _PAGE_MARGIN, -_PAGE_MARGIN, -_PAGE_MARGIN)
    more = True
    pages = 0
    try:
        while more:
            if pages >= _MAX_PDF_PAGES:
                raise ConversionError("Document is too large to render as PDF.")
            device = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(device)
            writer.end_page()
            pages += 1
    finally:
        writer.close()


def _html_to_pdf_sync(html: str, output_path: str) -> None:
    candidates = [html]
    without_images = re.sub(r"<img\b[^>]*>", "", html, flags=re.IGNORECASE)
    if without_images != html:
        candidates.append(without_images)

    last_error: Optional[Exception] = None
    for candidate in candidates:
        try:
            _render_pdf(candidate, output_path)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return
        except Exception as exc:  # retry with images stripped
            last_error = exc
            if os.path.exists(output_path):
                os.remove(output_path)

    raise ConversionError(f"PDF rendering failed: {last_error}")


def _epub_to_pdf_sync(input_path: str, output_path: str) -> None:
    try:
        document = fitz.open(input_path)
    except Exception as exc:
        raise ConversionError("Invalid or corrupt EPUB file.") from exc

    try:
        pdf_bytes = document.convert_to_pdf()
    except Exception as exc:
        raise ConversionError(
            "Could not parse this EPUB. It may be DRM-protected, corrupt, or "
            "use unsupported features."
        ) from exc
    finally:
        document.close()

    pdf_document = fitz.open("pdf", pdf_bytes)
    try:
        if pdf_document.page_count == 0:
            raise ConversionError("Invalid or empty EPUB file.")
        pdf_document.save(output_path)
    finally:
        pdf_document.close()


def _to_xhtml(body: str, title: str) -> str:
    full = _wrap_html(body, title)
    try:
        from lxml import etree, html as lxml_html

        tree = lxml_html.fromstring(full)
        xml = etree.tostring(tree, method="xml", encoding="unicode")
    except Exception:
        xml = (
            "<html><head><title>{}</title></head><body><pre>{}</pre></body></html>".format(
                html_lib.escape(title), html_lib.escape(_html_to_text(body))
            )
        )

    opening = xml.split(">", 1)[0]
    if "xmlns=" not in opening:
        xml = re.sub(
            r"<html([^>]*)>",
            r'<html\1 xmlns="http://www.w3.org/1999/xhtml">',
            xml,
            count=1,
        )
    if not xml.lstrip().startswith("<?xml"):
        xml = '<?xml version="1.0" encoding="utf-8"?>\n' + xml
    return xml


def _build_epub_sync(body: str, title: str, output_path: str) -> None:
    book_id = f"urn:uuid:{uuid.uuid4()}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    escaped_title = html_lib.escape(title or "Converted document")
    content = _to_xhtml(body, title or "Converted document")

    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><head>'
        f"<title>{escaped_title}</title></head><body>"
        f'<nav epub:type="toc"><h1>{escaped_title}</h1>'
        '<ol><li><a href="content.xhtml">Content</a></li></ol>'
        "</nav></body></html>"
    )

    package = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="bookid">{book_id}</dc:identifier>'
        f"<dc:title>{escaped_title}</dc:title>"
        "<dc:language>en</dc:language>"
        f'<meta property="dcterms:modified">{modified}</meta>'
        "</metadata><manifest>"
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        '<item id="content" href="content.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest><spine><itemref idref=\"content\"/></spine></package>"
    )

    container = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        "<rootfiles><rootfile full-path=\"OEBPS/content.opf\" "
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )

    with zipfile.ZipFile(output_path, "w") as archive:
        mimetype = zipfile.ZipInfo("mimetype")
        mimetype.compress_type = zipfile.ZIP_STORED
        archive.writestr(mimetype, "application/epub+zip")
        archive.writestr("META-INF/container.xml", container, zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/content.opf", package, zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/content.xhtml", content, zipfile.ZIP_DEFLATED)


def _pdf_or_epub_to_markdown_sync(path: str, fallback_html: str) -> str:
    try:
        markdown_text = pymupdf4llm.to_markdown(path)
        if markdown_text and markdown_text.strip():
            return markdown_text.strip()
    except Exception:
        pass
    text = _html_to_text(fallback_html)
    return text


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


async def convert(
    input_path: str,
    output_path: str,
    source_format: str,
    target_format: str,
) -> None:
    src = source_format.lower().strip(".")
    tgt = target_format.lower().strip(".")

    if src == tgt:
        await _run_cpu(shutil.copyfile, input_path, output_path)
        _verify_output(output_path)
        return

    if tgt == "pdf":
        if src == "epub":
            await _run_cpu(_epub_to_pdf_sync, input_path, output_path)
        else:
            html = await _run_cpu(_source_to_html_sync, src, input_path)
            await _run_cpu(_html_to_pdf_sync, html, output_path)

    elif tgt == "docx":
        html = await _run_cpu(_source_to_html_sync, src, input_path)
        await _run_cpu(_html_to_docx_sync, html, output_path)

    elif tgt == "html":
        if src == "html":
            await _run_cpu(shutil.copyfile, input_path, output_path)
        else:
            body = await _run_cpu(_source_to_html_sync, src, input_path)
            title = Path(input_path).stem
            await _run_cpu(
                lambda: _write_text(output_path, _wrap_html(body, title))
            )

    elif tgt == "txt":
        if src == "docx":
            text = await _run_cpu(_docx_to_text_sync, input_path)
        elif src in ("pdf", "epub"):
            text = await _run_cpu(_fitz_extract_text_sync, input_path)
        else:
            body = await _run_cpu(_source_to_html_sync, src, input_path)
            text = await _run_cpu(_html_to_text, body)
        await _run_cpu(_write_text, output_path, text)

    elif tgt == "md":
        if src == "docx":
            text = await _run_cpu(_docx_to_markdown_sync, input_path)
        elif src in ("pdf", "epub"):
            body = await _run_cpu(_source_to_html_sync, src, input_path)
            text = await _run_cpu(_pdf_or_epub_to_markdown_sync, input_path, body)
        elif src == "txt":
            text = await _run_cpu(_read_text, input_path)
        else:
            body = await _run_cpu(_source_to_html_sync, src, input_path)
            text = await _run_cpu(_html_to_markdown, body)
        await _run_cpu(_write_text, output_path, text)

    elif tgt == "epub":
        body = await _run_cpu(_source_to_html_sync, src, input_path)
        title = Path(input_path).stem
        await _run_cpu(_build_epub_sync, body, title, output_path)

    else:
        raise ConversionError(f"Unknown target format: .{tgt}")

    _verify_output(output_path)


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _verify_output(path: str) -> None:
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        raise ConversionError("Conversion produced an empty output file.")

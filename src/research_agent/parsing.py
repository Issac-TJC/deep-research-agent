"""Deterministic text locators. PDF figures/OCR and reliable scientific math reconstruction are out of scope."""

import io
import re

import pdfplumber
from bs4 import BeautifulSoup

from research_agent.contracts import ParsedDocument, TextBlock

PARSER_VERSION = "text-locators-v2-flow"


def parse_document(raw: bytes, mime: str, title: str = "Source", max_chars=600000) -> ParsedDocument:
    blocks: list[TextBlock] = []
    pieces: list[str] = []
    warnings: list[str] = []
    offset = 0

    def append(text, page=None, bbox=None, kind="paragraph"):
        nonlocal offset
        if not text.strip():
            return
        if offset + len(text) > max_chars:
            raise ValueError("parsed_document_exceeds_limit")
        blocks.append(
            TextBlock(text=text, start=offset, end=offset + len(text), page=page, bbox=bbox, kind=kind)
        )
        pieces.append(text)
        offset += len(text) + 2

    if mime == "application/pdf":
        if not raw.startswith(b"%PDF-"):
            raise ValueError("invalid_pdf_signature")
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            if len(pdf.pages) > 150:
                raise ValueError("pdf_page_limit")
            for i, page in enumerate(pdf.pages, 1):
                text = page.extract_text(layout=False, use_text_flow=True, x_tolerance=1) or ""
                if not text.strip():
                    warnings.append(f"page {i}: no extractable text; figures/scans not interpreted")
                append(text, i, (0, 0, float(page.width), float(page.height)), "page")
                for table in page.find_tables():
                    rows = table.extract()
                    text = "\n".join(
                        " | ".join((cell or "").replace("\n", " ") for cell in row) for row in rows
                    )
                    append(text, i, tuple(table.bbox), "table")
        warnings.append(
            "PDF equations, figures and table structure require visual confirmation; no OCR performed"
        )
    elif mime in {"text/html", "text/plain", "text/markdown"}:
        text = raw.decode("utf-8", errors="strict")
        if mime == "text/html":
            soup = BeautifulSoup(text, "lxml")
            if soup.title:
                title = soup.title.get_text(" ", strip=True)[:500]
            for tag in soup(["script", "style", "noscript", "nav", "footer", "form"]):
                tag.decompose()
            article = soup.find("article") or soup.find("main") or soup.body or soup
            text = article.get_text("\n", strip=True)
        for paragraph in re.split(r"\n\s*\n", text):
            append(paragraph.strip())
    else:
        raise ValueError("unsupported_mime")
    if not blocks:
        raise ValueError("empty_or_scanned_document")
    return ParsedDocument(title=title, text="\n\n".join(pieces), blocks=blocks, warnings=warnings)

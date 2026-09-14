"""Deterministic native locators plus an optional local Docling PDF pipeline."""

import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

import pdfplumber
from bs4 import BeautifulSoup

from research_agent.contracts import ParsedDocument, TextBlock

PARSER_VERSION = "text-locators-v3-multimodal"


def parse_document(raw: bytes, mime: str, title: str = "Source", max_chars=600000) -> ParsedDocument:
    blocks: list[TextBlock] = []
    pieces: list[str] = []
    warnings: list[str] = []
    offset = 0

    def append(
        text,
        page=None,
        bbox=None,
        kind="paragraph",
        section_path=None,
        extraction_method="native",
        confidence=None,
        table_cells=None,
        crop_key=None,
    ):
        nonlocal offset
        if not text.strip():
            return
        if offset + len(text) > max_chars:
            raise ValueError("parsed_document_exceeds_limit")
        blocks.append(
            TextBlock(
                id=hashlib.sha256(f"{offset}:{page}:{kind}:{text}".encode()).hexdigest()[:24],
                text=text,
                start=offset,
                end=offset + len(text),
                page=page,
                bbox=bbox,
                kind=kind,
                section_path=section_path or [],
                extraction_method=extraction_method,
                confidence=confidence,
                table_cells=table_cells or [],
                crop_key=crop_key,
            )
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
        section_path = []
        for paragraph in re.split(r"\n\s*\n", text):
            paragraph = paragraph.strip()
            heading = re.match(r"^(#{1,6})\s+(.+)$", paragraph)
            if heading:
                level, title_text = len(heading.group(1)), heading.group(2).strip()
                section_path = section_path[: level - 1] + [title_text]
                append(title_text, kind="heading", section_path=section_path)
            else:
                append(paragraph, section_path=section_path)
    else:
        raise ValueError("unsupported_mime")
    if not blocks:
        raise ValueError("empty_or_scanned_document")
    return ParsedDocument(title=title, text="\n\n".join(pieces), blocks=blocks, warnings=warnings)


def parse_document_enhanced(raw: bytes, title: str = "Source", max_chars=600000) -> ParsedDocument:
    """Convert a PDF with local Docling models, retaining page/bbox provenance where available."""
    if not raw.startswith(b"%PDF-"):
        raise ValueError("invalid_pdf_signature")
    with pdfplumber.open(io.BytesIO(raw)) as native_pdf:
        if len(native_pdf.pages) > 150:
            raise ValueError("pdf_page_limit")
        ocr_pages = {
            index
            for index, page in enumerate(native_pdf.pages, 1)
            if not (page.extract_text() or "").strip()
        }
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            HeadingHierarchyOptions,
            PdfPipelineOptions,
            RapidOcrOptions,
            TableFormerMode,
            smolvlm_picture_description,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise RuntimeError("docling_not_installed") from exc

    options = PdfPipelineOptions(
        enable_remote_services=False,
        document_timeout=300,
        artifacts_path=os.environ.get("DOCLING_ARTIFACTS_PATH"),
    )
    options.do_ocr = True
    options.ocr_options = RapidOcrOptions(lang=["zh-Hans", "en"])
    options.do_table_structure = True
    options.table_structure_options.mode = TableFormerMode.ACCURATE
    options.do_formula_enrichment = True
    options.generate_picture_images = True
    options.images_scale = 2.0
    options.do_picture_classification = True
    options.do_picture_description = True
    options.do_chart_extraction = True
    options.heading_hierarchy_options = HeadingHierarchyOptions(enabled=True)
    options.generate_parsed_pages = True
    options.picture_description_options = smolvlm_picture_description
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    with tempfile.TemporaryDirectory(prefix="research-docling-") as directory:
        path = Path(directory) / "source.pdf"
        path.write_bytes(raw)
        document = converter.convert(path).document

    labels = {
        "section_header": "heading",
        "title": "heading",
        "paragraph": "paragraph",
        "text": "paragraph",
        "list_item": "list",
        "code": "code",
        "table": "table",
        "formula": "formula",
        "picture": "figure",
        "caption": "caption",
    }
    raw_items = []
    for order, pair in enumerate(document.iterate_items()):
        doc_item = pair[0] if isinstance(pair, tuple) else pair
        item = doc_item.model_dump(mode="json")
        label = str(item.get("label") or "paragraph")
        value = item.get("text") or item.get("orig") or ""
        if label == "table" and hasattr(doc_item, "export_to_markdown"):
            value = doc_item.export_to_markdown(doc=document)
        if label == "picture":
            annotations = item.get("annotations") or []
            descriptions = [
                part.get("text", "") for part in annotations if isinstance(part, dict)
            ]
            captions = item.get("captions") or []
            value = " ".join(descriptions) or " ".join(
                str(part.get("text", part)) if isinstance(part, dict) else str(part)
                for part in captions
            )
            if not value:
                value = " ".join(
                    json.dumps(part, ensure_ascii=False, sort_keys=True)
                    if isinstance(part, dict)
                    else str(part)
                    for part in annotations
                )
        provenance = (item.get("prov") or [{}])[0]
        page = provenance.get("page_no") or provenance.get("page")
        bbox_value = provenance.get("bbox")
        if isinstance(bbox_value, dict):
            bbox = tuple(float(bbox_value.get(key, 0)) for key in ("l", "t", "r", "b"))
        elif isinstance(bbox_value, (list, tuple)) and len(bbox_value) == 4:
            bbox = tuple(float(coordinate) for coordinate in bbox_value)
        else:
            bbox = None
        raw_items.append((order, int(page or 0), label, str(value), bbox, item))
    if not raw_items:
        raise ValueError("enhanced_parser_empty")

    blocks, pieces, offset, sections = [], [], 0, []
    warnings = []
    for _, page, label, value, bbox, item in raw_items:
        value = value.strip()
        if not value:
            continue
        if offset + len(value) > max_chars:
            raise ValueError("parsed_document_exceeds_limit")
        kind = labels.get(label, "paragraph")
        if kind == "heading":
            level = max(1, int(item.get("level") or item.get("level_no") or 1))
            sections = sections[: level - 1] + [value]
        method = "ocr" if page in ocr_pages or item.get("ocr") else "layout"
        if kind == "formula":
            method = "formula_recognition"
        elif kind == "figure":
            method = "vision"
        confidence = item.get("confidence") or (item.get("prov") or [{}])[0].get("confidence")
        if confidence is None and method in {"ocr", "formula_recognition", "vision"}:
            # Unknown model confidence must not silently pass the 0.80 confirmation gate.
            confidence = 0.79
        table_cells = (
            item.get("annotations", [])
            if kind == "figure"
            else item.get("data", {}).get("table_cells", [])
            if isinstance(item.get("data"), dict)
            else []
        )
        blocks.append(
            TextBlock(
                id=hashlib.sha256(f"{offset}:{page}:{kind}:{value}".encode()).hexdigest()[:24],
                text=value,
                start=offset,
                end=offset + len(value),
                page=page or None,
                bbox=bbox,
                kind=kind,
                section_path=list(sections),
                extraction_method=method,
                confidence=float(confidence) if confidence is not None else None,
                table_cells=table_cells,
            )
        )
        pieces.append(value)
        offset += len(value) + 2
    if not blocks:
        raise ValueError("empty_or_scanned_document")
    warnings.append("Enhanced local parsing used; OCR, formulas, figures and tables may require visual confirmation")
    return ParsedDocument(
        title=title,
        text="\n\n".join(pieces),
        blocks=blocks,
        warnings=warnings,
        capabilities=["text", "locators", "ocr", "layout", "tables", "formulas", "figures", "charts"],
        reading_order_version="docling-v1",
    )

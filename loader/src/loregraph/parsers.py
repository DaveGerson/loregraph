from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .classification import classify_text
from .config import ParsingConfig
from .ids import normalize_text, stable_id
from .models import Block, SourceSpec


@dataclass(slots=True)
class ParseResult:
    parser_name: str
    page_count: int | None
    blocks: list[Block]


def parse_document(
    source: SourceSpec,
    source_id: str,
    config: ParsingConfig,
) -> ParseResult:
    path = Path(source.path)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.casefold()
    if suffix in {".md", ".markdown", ".txt"}:
        text = path.read_text(encoding="utf-8")
        blocks = _parse_markdown_text(text, source_id)
        return _apply_page_exclusions(
            ParseResult("markdown", _max_page(blocks), blocks), source.excludes
        )
    if suffix != ".pdf":
        raise ValueError(f"Unsupported input format: {suffix}")

    backend = config.backend.casefold()
    if backend in {"auto", "docling"}:
        try:
            return _apply_page_exclusions(
                _parse_docling(path, source_id, config), source.excludes
            )
        except ImportError:
            if backend == "docling":
                raise RuntimeError("Docling backend requested but docling is not installed") from None
        except Exception:
            if backend == "docling":
                raise
            # Auto mode intentionally falls back. The parser name and quality
            # report make this visible rather than silently claiming Docling.
    if backend not in {"auto", "pymupdf", "docling"}:
        raise ValueError(f"Unknown parser backend: {backend}")
    return _apply_page_exclusions(
        _parse_pymupdf(path, source_id, config), source.excludes
    )


def _parse_markdown_text(text: str, source_id: str) -> list[Block]:
    page = 1
    headings: list[str] = []
    blocks: list[Block] = []
    buffer: list[str] = []
    buffer_page = page
    order = 0

    def flush(hint: str | None = None) -> None:
        nonlocal buffer, buffer_page, order
        raw = "\n".join(buffer).strip()
        buffer = []
        if not raw:
            return
        content_type, atomic = classify_text(raw, hint)
        blocks.append(
            Block(
                id=stable_id("ingest", "block", source_id, order, raw),
                source_id=source_id,
                order=order,
                page_start=buffer_page,
                page_end=page,
                text=raw,
                heading_path=list(headings),
                content_type=content_type,
                atomic=atomic,
                metadata={"parser_hint": hint or "paragraph"},
            )
        )
        order += 1

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        page_match = re.fullmatch(r"\s*<!--\s*page\s*:\s*(\d+)\s*-->\s*", line, re.I)
        if page_match:
            flush()
            page = int(page_match.group(1))
            buffer_page = page
            index += 1
            continue
        if "\f" in line:
            parts = line.split("\f")
            for part_index, part in enumerate(parts):
                if part.strip():
                    buffer.append(part)
                if part_index < len(parts) - 1:
                    flush()
                    page += 1
                    buffer_page = page
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            flush()
            level = len(heading.group(1))
            title = normalize_text(heading.group(2))
            headings = headings[: level - 1]
            while len(headings) < level - 1:
                headings.append("Untitled")
            headings.append(title)
            index += 1
            continue
        if line.count("|") >= 2:
            flush()
            buffer_page = page
            table_lines = [line]
            index += 1
            while index < len(lines) and lines[index].count("|") >= 2:
                table_lines.append(lines[index])
                index += 1
            buffer = table_lines
            flush("table")
            buffer_page = page
            continue
        if not line.strip():
            flush()
            buffer_page = page
        else:
            if not buffer:
                buffer_page = page
            buffer.append(line.rstrip())
        index += 1
    flush()
    return blocks


def _parse_pymupdf(path: Path, source_id: str, config: ParsingConfig) -> ParseResult:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for the pymupdf parser") from exc

    raw_blocks: list[dict[str, Any]] = []
    font_sizes: list[float] = []
    with fitz.open(path) as pdf:
        page_count = pdf.page_count
        for page_index, page in enumerate(pdf):
            height = float(page.rect.height or 1)
            data = page.get_text("dict", sort=True)
            for block in data.get("blocks", []):
                if block.get("type") != 0:
                    continue
                lines: list[str] = []
                sizes: list[float] = []
                bold = False
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    text = "".join(span.get("text", "") for span in spans).strip()
                    if text:
                        lines.append(text)
                    for span in spans:
                        size = float(span.get("size", 0.0) or 0.0)
                        if size:
                            sizes.append(size)
                            font_sizes.append(size)
                        bold = bold or "bold" in str(span.get("font", "")).casefold()
                text = "\n".join(lines).strip()
                if not text:
                    continue
                bbox = block.get("bbox", (0, 0, 0, 0))
                raw_blocks.append(
                    {
                        "page": page_index + 1,
                        "text": text,
                        "max_size": max(sizes, default=0.0),
                        "bold": bold,
                        "top_ratio": float(bbox[1]) / height,
                        "bottom_ratio": float(bbox[3]) / height,
                    }
                )

    median_size = statistics.median(font_sizes) if font_sizes else 10.0
    repeated = _repeated_margin_keys(raw_blocks, page_count) if config.remove_repeated_margins else set()
    headings: list[str] = []
    blocks: list[Block] = []
    order = 0
    for raw in raw_blocks:
        margin_key = _margin_key(raw)
        if margin_key and margin_key in repeated:
            continue
        text = raw["text"]
        heading_level = _pdf_heading_level(raw, median_size)
        if heading_level:
            title = normalize_text(text)
            headings = headings[: heading_level - 1]
            while len(headings) < heading_level - 1:
                headings.append("Untitled")
            headings.append(title)
            continue
        content_type, atomic = classify_text(text)
        blocks.append(
            Block(
                id=stable_id("ingest", "block", source_id, order, text),
                source_id=source_id,
                order=order,
                page_start=raw["page"],
                page_end=raw["page"],
                text=text,
                heading_path=list(headings),
                content_type=content_type,
                atomic=atomic,
                metadata={"parser_hint": "pdf_text_block"},
            )
        )
        order += 1
    return ParseResult("pymupdf", page_count, blocks)


def _parse_docling(path: Path, source_id: str, config: ParsingConfig) -> ParseResult:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError:
        raise

    options = PdfPipelineOptions()
    options.do_ocr = config.ocr
    options.do_table_structure = True
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    document = converter.convert(source=path).document
    headings: list[str] = []
    blocks: list[Block] = []
    order = 0
    max_page = 1
    for item, level in document.iterate_items():
        text = str(getattr(item, "text", "") or "").strip()
        if not text and hasattr(item, "export_to_markdown"):
            text = str(item.export_to_markdown(document)).strip()
        if not text:
            continue
        label = str(getattr(item, "label", "")).casefold().split(".")[-1]
        prov = getattr(item, "prov", None) or []
        pages = [int(getattr(entry, "page_no", 1) or 1) for entry in prov]
        page_start, page_end = (min(pages), max(pages)) if pages else (1, 1)
        max_page = max(max_page, page_end)
        if "section_header" in label or label.endswith("title"):
            heading_level = max(1, int(level or 1))
            headings = headings[: heading_level - 1]
            while len(headings) < heading_level - 1:
                headings.append("Untitled")
            headings.append(normalize_text(text))
            continue
        parser_hint = "table" if "table" in label else "picture" if "picture" in label else None
        content_type, atomic = classify_text(text, parser_hint)
        blocks.append(
            Block(
                id=stable_id("ingest", "block", source_id, order, text),
                source_id=source_id,
                order=order,
                page_start=page_start,
                page_end=page_end,
                text=text,
                heading_path=list(headings),
                content_type=content_type,
                atomic=atomic,
                metadata={"parser_hint": label or "docling_item"},
            )
        )
        order += 1
    if not blocks:
        raise ValueError("Docling produced no text blocks")
    return ParseResult("docling", max_page, blocks)


def _pdf_heading_level(raw: dict[str, Any], median_size: float) -> int | None:
    text = normalize_text(raw["text"])
    if len(text) > 140 or text.endswith(('.', '!', '?')):
        return None
    ratio = float(raw["max_size"] or 0.0) / max(median_size, 1.0)
    if ratio >= 1.70:
        return 1
    if ratio >= 1.40:
        return 2
    if ratio >= 1.20 or (raw["bold"] and ratio >= 1.05):
        return 3
    return None


def _margin_key(raw: dict[str, Any]) -> str | None:
    if raw["top_ratio"] > 0.10 and raw["bottom_ratio"] < 0.90:
        return None
    text = re.sub(r"\d+", "#", normalize_text(raw["text"]).casefold())
    return text if 1 <= len(text) <= 120 else None


def _repeated_margin_keys(raw_blocks: list[dict[str, Any]], page_count: int) -> set[str]:
    keys_per_page: dict[int, set[str]] = {}
    for raw in raw_blocks:
        key = _margin_key(raw)
        if key:
            keys_per_page.setdefault(raw["page"], set()).add(key)
    counts = Counter(key for keys in keys_per_page.values() for key in keys)
    threshold = max(3, int(page_count * 0.50))
    return {key for key, count in counts.items() if count >= threshold}


def _max_page(blocks: list[Block]) -> int | None:
    return max((block.page_end for block in blocks), default=None)


def _apply_page_exclusions(result: ParseResult, ranges: list[list[int]]) -> ParseResult:
    normalized: list[tuple[int, int]] = []
    for value in ranges:
        if len(value) != 2:
            raise ValueError("Each excludes entry must be [start_page, end_page]")
        start, end = int(value[0]), int(value[1])
        if start < 1 or end < start:
            raise ValueError(f"Invalid excluded page range: {value}")
        normalized.append((start, end))
    if not normalized:
        return result
    blocks = [
        block
        for block in result.blocks
        if not any(block.page_start <= end and start <= block.page_end for start, end in normalized)
    ]
    return ParseResult(result.parser_name, result.page_count, blocks)

from __future__ import annotations

import re
from dataclasses import dataclass

from .classification import profile_for
from .config import ChunkingConfig
from .ids import sha256_bytes, stable_id
from .models import Block, Chunk, SourceSpec


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate suitable for preflight budgets."""

    words = re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE)
    return max(1, int(len(words) * 1.18))


@dataclass(slots=True)
class _Unit:
    text: str
    page_start: int
    page_end: int
    heading_path: list[str]
    content_type: str
    atomic: bool


def build_chunks(
    blocks: list[Block],
    source: SourceSpec,
    source_id: str,
    config: ChunkingConfig,
) -> list[Chunk]:
    units: list[_Unit] = []
    for block in sorted(blocks, key=lambda item: item.order):
        if block.atomic or estimate_tokens(block.text) <= config.max_tokens:
            units.append(
                _Unit(
                    block.text,
                    block.page_start,
                    block.page_end,
                    block.heading_path,
                    block.content_type,
                    block.atomic,
                )
            )
        else:
            for piece in _split_oversized(block.text, config.max_tokens):
                units.append(
                    _Unit(
                        piece,
                        block.page_start,
                        block.page_end,
                        block.heading_path,
                        block.content_type,
                        False,
                    )
                )

    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    current_tokens = 0
    for unit in units:
        tokens = estimate_tokens(unit.text)
        boundary = bool(
            current
            and (
                unit.atomic
                or current[-1].atomic
                or unit.heading_path != current[-1].heading_path
                or unit.content_type != current[-1].content_type
                or current_tokens + tokens > config.max_tokens
            )
        )
        if boundary:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(unit)
        current_tokens += tokens
        if unit.atomic or current_tokens >= config.target_tokens:
            groups.append(current)
            current = []
            current_tokens = 0
    if current:
        groups.append(current)

    chunks: list[Chunk] = []
    for index, group in enumerate(groups):
        raw_text = "\n\n".join(unit.text.strip() for unit in group if unit.text.strip()).strip()
        if not raw_text:
            continue
        heading_path = group[0].heading_path
        breadcrumb_parts = [source.code, *heading_path]
        breadcrumb = " > ".join(part for part in breadcrumb_parts if part)
        text = f"[{breadcrumb}]\n{raw_text}" if breadcrumb else raw_text
        text_hash = sha256_bytes(text.encode("utf-8"))
        content_type = group[0].content_type
        profile = profile_for(source, content_type)
        token_count = estimate_tokens(text)
        eligible = (
            source.indexing_mode not in {"NO_INDEX", "METADATA_ONLY"}
            and content_type not in {"fiction", "image", "toc", "index", "legal"}
            and token_count >= config.min_tokens
        )
        chunk_id = stable_id("global", "chunk", source_id, index, text_hash)
        chunks.append(
            Chunk(
                id=chunk_id,
                source_id=source_id,
                chunk_index=index,
                text=text,
                raw_text=raw_text,
                text_hash=text_hash,
                token_count=token_count,
                page_start=min(unit.page_start for unit in group),
                page_end=max(unit.page_end for unit in group),
                heading_path=list(heading_path),
                content_type=content_type,
                profile=profile,
                actuality=source.actuality,
                canon_scope=source.canon_scope,
                visibility=source.visibility,
                indexing_mode=source.indexing_mode,
                extraction_eligible=eligible,
                remote_processing_allowed=source.remote_processing_allowed,
                campaign_id=source.campaign_id,
                game_system=source.game_system,
                ruleset=source.ruleset,
                ruleset_version=source.ruleset_version,
                metadata={
                    "atomic": any(unit.atomic for unit in group),
                    "claims_only": source.source_kind == "fiction",
                },
            )
        )
    for index, chunk in enumerate(chunks):
        chunk.previous_chunk_id = chunks[index - 1].id if index else None
        chunk.next_chunk_id = chunks[index + 1].id if index + 1 < len(chunks) else None
    return chunks


def _split_oversized(text: str, max_tokens: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(paragraphs) == 1:
        paragraphs = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
            if part.strip()
        ]
    output: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for paragraph in paragraphs:
        tokens = estimate_tokens(paragraph)
        if tokens > max_tokens:
            words = paragraph.split()
            per_piece = max(40, int(max_tokens / 1.18))
            pieces = [" ".join(words[i : i + per_piece]) for i in range(0, len(words), per_piece)]
        else:
            pieces = [paragraph]
        for piece in pieces:
            piece_tokens = estimate_tokens(piece)
            if current and current_tokens + piece_tokens > max_tokens:
                output.append("\n\n".join(current))
                current = []
                current_tokens = 0
            current.append(piece)
            current_tokens += piece_tokens
    if current:
        output.append("\n\n".join(current))
    return output or [text]

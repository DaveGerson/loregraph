from __future__ import annotations

import re

from .models import SourceSpec


STAT_BLOCK_MARKERS = (
    r"\barmor class\b",
    r"\bhit points\b",
    r"\bspeed\b",
    r"\bchallenge(?: rating)?\b",
    r"\bSTR\s+DEX\s+CON\s+INT\s+WIS\s+CHA\b",
    r"\bsaving throws\b",
)

SPELL_MARKERS = (
    r"\bcasting time\b",
    r"\brange\b",
    r"\bcomponents\b",
    r"\bduration\b",
)


def classify_text(text: str, parser_hint: str | None = None) -> tuple[str, bool]:
    """Return (content_type, atomic).

    The heuristic deliberately favors false negatives over false positives. A
    normal lore paragraph accidentally treated as an atomic stat block harms
    retrieval more than leaving one unusual stat block for manual review.
    """

    if parser_hint in {"table", "picture", "formula", "code"}:
        mapped = {"picture": "image", "formula": "rules", "code": "rules"}
        kind = mapped.get(parser_hint, parser_hint)
        return kind, kind in {"table", "rules"}

    compact = re.sub(r"\s+", " ", text).strip()
    lower = compact.casefold()
    stat_score = sum(bool(re.search(pattern, compact, re.IGNORECASE)) for pattern in STAT_BLOCK_MARKERS)
    if stat_score >= 4:
        return "statblock", True

    spell_score = sum(bool(re.search(pattern, compact, re.IGNORECASE)) for pattern in SPELL_MARKERS)
    if spell_score >= 3 and re.search(r"\b(cantrip|[1-9](?:st|nd|rd|th)[ -]level)\b", lower):
        return "spell", True

    if _looks_like_markdown_table(text):
        return "table", True
    if re.search(r"\broll\s+d(?:100|20|12|10|8|6|4)\b", lower) and "result" in lower:
        return "table", True
    if re.search(r"\bprerequisite\s*:", lower) or "at higher levels" in lower:
        return "rules", True
    if re.search(r"\b(read aloud|boxed text)\b", lower):
        return "adventure", True
    if re.search(r"\bchapter\s+\d+\b", lower) and len(compact) < 140:
        return "heading", True
    if re.search(r"\b(index|table of contents)\b", lower) and len(compact) < 80:
        return "toc", False
    return "lore", False


def profile_for(source: SourceSpec, content_type: str) -> str:
    if source.extraction_profile:
        return source.extraction_profile
    if source.source_kind == "campaign":
        return "campaign"
    if source.source_kind in {"note", "document", "web"}:
        return "notes"
    if source.source_kind == "adventure" or content_type == "adventure":
        return "adventure"
    if source.source_kind == "rules" or content_type in {"statblock", "spell", "rules", "item"}:
        return "rules"
    return "lore"


def _looks_like_markdown_table(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return len(lines) >= 2 and sum(line.count("|") >= 2 for line in lines) >= 2

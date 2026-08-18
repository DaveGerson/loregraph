from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ids import normalize_name, normalize_text, sha256_bytes, stable_id
from .models import AssertionRecord, Chunk, EntityRecord, EvidenceRecord, ReviewItem


@dataclass(slots=True)
class StructuredExtraction:
    entities: list[EntityRecord] = field(default_factory=list)
    assertions: list[AssertionRecord] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    review_items: list[ReviewItem] = field(default_factory=list)


def extract_structured_mechanics(chunk: Chunk, tenant_id: str) -> StructuredExtraction:
    if chunk.content_type == "statblock":
        return _extract_stat_block(chunk, tenant_id)
    if chunk.content_type == "spell":
        return _extract_spell(chunk, tenant_id)
    return StructuredExtraction()


def _extract_stat_block(chunk: Chunk, tenant_id: str) -> StructuredExtraction:
    name = _entry_name(chunk)
    if not name:
        return _name_review(chunk, "stat block")
    fields = {
        "armor_class": _first_int(r"\bArmor Class\s+(\d+)", chunk.raw_text),
        "hit_points": _first_int(r"\bHit Points\s+(\d+)", chunk.raw_text),
        "speed": _first_text(r"\bSpeed\s+([^\n]+)", chunk.raw_text),
        "challenge": _first_text(r"\bChallenge(?: Rating)?\s+([^\n]+)", chunk.raw_text),
    }
    type_line = next(
        (
            normalize_text(line)
            for line in chunk.raw_text.splitlines()[:5]
            if re.search(r"\b(tiny|small|medium|large|huge|gargantuan)\b", line, re.I)
        ),
        "",
    )
    fields["type_line"] = type_line or None
    namespace = tenant_id if chunk.campaign_id else "global"
    identity_scope = chunk.campaign_id or "published"
    creature_id = stable_id(
        namespace, "creature-archetype", identity_scope, normalize_name(name)
    )
    stat_id = stable_id(
        namespace, "stat-block", identity_scope, chunk.source_id, normalize_name(name)
    )
    creature = EntityRecord(
        id=creature_id,
        entity_type="CreatureArchetype",
        subtype="other",
        canonical_name=name,
        aliases=[],
        scope="CAMPAIGN" if chunk.campaign_id else "GLOBAL",
        campaign_id=chunk.campaign_id,
        confidence=0.95,
        provisional=True,
        source_chunk_ids=[chunk.id],
        metadata={"deterministic_parser": "statblock-v1"},
    )
    stat = EntityRecord(
        id=stat_id,
        entity_type="StatBlock",
        subtype="stat-block",
        canonical_name=f"{name} stat block",
        aliases=[name],
        scope="CAMPAIGN" if chunk.campaign_id else "GLOBAL",
        campaign_id=chunk.campaign_id,
        confidence=0.98,
        provisional=True,
        source_chunk_ids=[chunk.id],
        metadata={
            "deterministic_parser": "statblock-v1",
            "structured": {key: value for key, value in fields.items() if value is not None},
            "source_id": chunk.source_id,
        },
    )
    evidence = _evidence(chunk)
    # Stat blocks are sourced mechanical representations of a creature
    # archetype; they are not RuleElementVersion nodes, so VERSION_OF would
    # violate the canonical domain/range contract.
    has_stat = _assertion(
        chunk,
        creature_id,
        "HAS_STAT_BLOCK",
        stat_id,
        evidence.id,
        confidence=0.95,
    )
    entities = [creature, stat]
    assertions = [has_stat]
    ruleset = _ruleset_entity(chunk)
    if ruleset:
        entities.append(ruleset)
        assertions.append(
            _assertion(
                chunk,
                stat_id,
                "FOR_RULESET_VERSION",
                ruleset.id,
                evidence.id,
                confidence=1.0,
            )
        )
    return StructuredExtraction(entities, assertions, [evidence])


def _extract_spell(chunk: Chunk, tenant_id: str) -> StructuredExtraction:
    name = _entry_name(chunk)
    if not name:
        return _name_review(chunk, "spell")
    fields = {
        "level_school": _first_text(
            r"\b((?:cantrip|[1-9](?:st|nd|rd|th)[ -]level)[^\n]*)", chunk.raw_text
        ),
        "casting_time": _first_text(r"\bCasting Time\s*:?\s*([^\n]+)", chunk.raw_text),
        "range": _first_text(r"\bRange\s*:?\s*([^\n]+)", chunk.raw_text),
        "components": _first_text(r"\bComponents\s*:?\s*([^\n]+)", chunk.raw_text),
        "duration": _first_text(r"\bDuration\s*:?\s*([^\n]+)", chunk.raw_text),
    }
    namespace = tenant_id if chunk.campaign_id else "global"
    identity_scope = chunk.campaign_id or "published"
    element_id = stable_id(
        namespace, "rule-element", identity_scope, "spell", normalize_name(name)
    )
    version_id = stable_id(
        namespace,
        "rule-element-version",
        identity_scope,
        chunk.source_id,
        "spell",
        normalize_name(name),
    )
    element = EntityRecord(
        id=element_id,
        entity_type="RuleElement",
        subtype="spell",
        canonical_name=name,
        aliases=[],
        scope="CAMPAIGN" if chunk.campaign_id else "GLOBAL",
        campaign_id=chunk.campaign_id,
        confidence=0.98,
        provisional=True,
        source_chunk_ids=[chunk.id],
        metadata={"deterministic_parser": "spell-v1"},
    )
    version = EntityRecord(
        id=version_id,
        entity_type="RuleElementVersion",
        subtype="spell",
        canonical_name=f"{name} ({chunk.source_id.rsplit(':', 1)[-1]})",
        aliases=[],
        scope="CAMPAIGN" if chunk.campaign_id else "GLOBAL",
        campaign_id=chunk.campaign_id,
        confidence=0.98,
        provisional=True,
        source_chunk_ids=[chunk.id],
        metadata={
            "deterministic_parser": "spell-v1",
            "structured": {key: value for key, value in fields.items() if value is not None},
            "source_id": chunk.source_id,
        },
    )
    evidence = _evidence(chunk)
    assertion = _assertion(
        chunk,
        version_id,
        "VERSION_OF",
        element_id,
        evidence.id,
        confidence=0.98,
    )
    entities = [element, version]
    assertions = [assertion]
    ruleset = _ruleset_entity(chunk)
    if ruleset:
        entities.append(ruleset)
        assertions.append(
            _assertion(
                chunk,
                version_id,
                "FOR_RULESET_VERSION",
                ruleset.id,
                evidence.id,
                confidence=1.0,
            )
        )
    return StructuredExtraction(entities, assertions, [evidence])


def _entry_name(chunk: Chunk) -> str | None:
    first = next((normalize_text(line) for line in chunk.raw_text.splitlines() if line.strip()), "")
    if first and not re.match(
        r"^(tiny|small|medium|large|huge|gargantuan|armor class|casting time)\b",
        first,
        re.I,
    ) and len(first) <= 120:
        return first
    if chunk.heading_path:
        candidate = normalize_text(chunk.heading_path[-1])
        if candidate.casefold() not in {
            "stat block",
            "statistics",
            "spells",
            "spell",
            "monsters",
            "creatures",
            "bestiary",
        }:
            return candidate
    return None


def _evidence(chunk: Chunk) -> EvidenceRecord:
    quote = chunk.raw_text[: min(700, len(chunk.raw_text))].rstrip()
    normalized = normalize_text(quote)
    return EvidenceRecord(
        id=stable_id("global", "fragment", chunk.id, "deterministic-mechanics"),
        chunk_id=chunk.id,
        source_id=chunk.source_id,
        quote=quote,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        exact_text_hash=sha256_bytes(normalized.encode("utf-8")),
    )


def _assertion(
    chunk: Chunk,
    subject_id: str,
    predicate: str,
    object_id: str,
    evidence_id: str,
    confidence: float,
) -> AssertionRecord:
    return AssertionRecord(
        id=stable_id("global", "assertion", chunk.id, subject_id, predicate, object_id),
        subject_id=subject_id,
        predicate_code=predicate,
        object_id=object_id,
        literal_value=None,
        truth_status="ASSERTED",
        canon_scope=chunk.canon_scope,
        actuality=chunk.actuality,
        confidence=confidence,
        review_status="UNREVIEWED",
        derivation_method="DETERMINISTIC_PARSE",
        source_chunk_ids=[chunk.id],
        evidence_ids=[evidence_id],
        campaign_id=chunk.campaign_id,
        visibility=chunk.visibility,
        metadata={"parser": "structured-mechanics-v1"},
    )


def _name_review(chunk: Chunk, kind: str) -> StructuredExtraction:
    review = ReviewItem(
        id=stable_id("global", "review", chunk.id, f"missing-{kind}-name"),
        item_type="STRUCTURED_PARSE",
        reason=f"Could not determine {kind} name from heading or first line",
        payload={"content_type": chunk.content_type, "pages": [chunk.page_start, chunk.page_end]},
        source_chunk_id=chunk.id,
    )
    return StructuredExtraction(review_items=[review])


def _ruleset_entity(chunk: Chunk) -> EntityRecord | None:
    if not chunk.ruleset_version:
        return None
    return EntityRecord(
        id=stable_id(
            "global", "ruleset-version", normalize_text(chunk.ruleset_version).casefold()
        ),
        entity_type="RulesetVersion",
        subtype="other",
        canonical_name=chunk.ruleset_version,
        aliases=[],
        scope="GLOBAL",
        confidence=1.0,
        provisional=False,
        source_chunk_ids=[chunk.id],
        metadata={"declared_by_manifest": True, "source_id": chunk.source_id},
    )


def _first_int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, re.I)
    return int(match.group(1)) if match else None


def _first_text(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.I)
    return normalize_text(match.group(1)) if match else None

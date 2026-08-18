from __future__ import annotations

import re

from .ids import normalize_text
from .llm import ExtractionPayload
from .models import Chunk
from .schema import SchemaRegistry


def quote_is_grounded(quote: str, chunk_text: str) -> bool:
    if not quote.strip():
        return False
    quote_normalized = normalize_text(quote).casefold()
    text_normalized = normalize_text(chunk_text).casefold()
    if quote_normalized in text_normalized:
        return True
    # PDF line-break hyphenation is a mechanical extraction artifact. Removing
    # only end-of-line hyphens keeps this fallback conservative.
    dehyphenated_quote = re.sub(r"(?<=\w)-\s+(?=\w)", "", quote_normalized)
    dehyphenated_text = re.sub(r"(?<=\w)-\s+(?=\w)", "", text_normalized)
    return dehyphenated_quote in dehyphenated_text


def validate_payload(
    payload: ExtractionPayload,
    chunk: Chunk,
    schema: SchemaRegistry,
) -> list[str]:
    errors: list[str] = []
    allowed_nodes = schema.allowed_node_types(chunk.profile)
    allowed_predicates = schema.allowed_predicates(chunk.profile)
    nodes = {node.temp_id: node for node in payload.nodes}
    if len(nodes) != len(payload.nodes):
        errors.append("node temp_id values must be unique")
    for node in payload.nodes:
        if node.entity_type not in allowed_nodes:
            errors.append(f"node {node.temp_id}: type {node.entity_type} not allowed")
        elif schema.node_types[node.entity_type].get("subtypes"):
            allowed_subtypes = set(schema.node_types[node.entity_type]["subtypes"])
            if node.subtype not in allowed_subtypes:
                errors.append(
                    f"node {node.temp_id}: subtype {node.subtype} not allowed for {node.entity_type}"
                )
        for item in node.properties:
            if not quote_is_grounded(item.source_quote, chunk.raw_text):
                errors.append(
                    f"node {node.temp_id}: property {item.name} quote is not grounded"
                )
    if chunk.metadata.get("claims_only") and payload.relationships:
        errors.append("claims-only chunk contains flat relationships")
    for rel in payload.relationships:
        if rel.predicate not in allowed_predicates:
            errors.append(f"relationship {rel.predicate}: predicate not allowed")
        if rel.subject not in nodes or rel.object not in nodes:
            errors.append(f"relationship {rel.predicate}: endpoint missing")
        elif not schema.pattern_allowed(
            chunk.profile,
            rel.predicate,
            nodes[rel.subject].entity_type,
            nodes[rel.object].entity_type,
        ):
            errors.append(f"relationship {rel.predicate}: domain/range violation")
        if not quote_is_grounded(rel.source_quote, chunk.raw_text):
            errors.append(f"relationship {rel.predicate}: source quote is not grounded")
    for claim in payload.claims:
        if claim.predicate not in allowed_predicates:
            errors.append(f"claim {claim.predicate}: predicate not allowed")
        if claim.subject not in nodes:
            errors.append(f"claim {claim.predicate}: subject missing")
        if claim.object is None and claim.literal_value is None:
            errors.append(f"claim {claim.predicate}: object or literal_value required")
        if claim.object is not None and claim.object not in nodes:
            errors.append(f"claim {claim.predicate}: object endpoint missing")
        elif (
            claim.subject in nodes
            and claim.object in nodes
            and claim.predicate in allowed_predicates
            and not schema.pattern_allowed(
                chunk.profile,
                claim.predicate,
                nodes[claim.subject].entity_type,
                nodes[claim.object].entity_type,
            )
        ):
            errors.append(f"claim {claim.predicate}: domain/range violation")
        if claim.literal_value is not None:
            errors.append(f"claim {claim.predicate}: literal object is not allowed by this profile")
        if claim.asserted_by is not None and claim.asserted_by not in nodes:
            errors.append(f"claim {claim.predicate}: asserted_by endpoint missing")
        if not quote_is_grounded(claim.source_quote, chunk.raw_text):
            errors.append(f"claim {claim.predicate}: source quote is not grounded")
    return errors

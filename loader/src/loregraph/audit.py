from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import LoaderConfig
from .models import Chunk
from .schema import SchemaRegistry
from .store import RecordStore
from .validation import quote_is_grounded


@dataclass(slots=True)
class AuditReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "counts": self.counts,
        }


def audit_store(config: LoaderConfig) -> AuditReport:
    store = RecordStore(config.workspace_dir)
    schema = SchemaRegistry(config.schema_path)
    report = AuditReport()
    sources = store.by_id("sources")
    chunks = store.by_id("chunks")
    entities = store.by_id("entities")
    mentions = store.by_id("mentions")
    evidence = store.by_id("evidence")
    assertions = store.by_id("assertions")
    reviews = store.by_id("review_queue")
    embeddings = store.by_id("embeddings")
    report.counts = {
        "sources": len(sources),
        "chunks": len(chunks),
        "entities": len(entities),
        "mentions": len(mentions),
        "evidence": len(evidence),
        "assertions": len(assertions),
        "open_reviews": sum(row.get("status") == "OPEN" for row in reviews.values()),
        "embeddings": len(embeddings),
    }

    material_versions: dict[str, list[dict[str, Any]]] = {}
    for source in sources.values():
        material_versions.setdefault(source["material_id"], []).append(source)
        if source.get("source_kind") == "rules" and not source.get("ruleset_version"):
            report.errors.append(f"Rules source {source['id']} lacks ruleset_version")
        if (
            source.get("indexing_mode") == "FULL_TEXT_WITH_ATTRIBUTION"
            and not str(source.get("attribution", "")).strip()
        ):
            report.errors.append(f"Source {source['id']} lacks required attribution")
    for material_id, versions in material_versions.items():
        current_count = sum(
            bool(row.get("metadata", {}).get("is_current")) for row in versions
        )
        if current_count != 1:
            report.errors.append(
                f"Material {material_id} has {current_count} current versions (expected one)"
            )

    for chunk in chunks.values():
        if chunk["source_id"] not in sources:
            report.errors.append(f"Chunk {chunk['id']} references missing source")
        if chunk["profile"] not in schema.profiles:
            report.errors.append(f"Chunk {chunk['id']} uses unknown profile")
        if chunk["token_count"] <= 0:
            report.errors.append(f"Chunk {chunk['id']} has invalid token count")
        if chunk["canon_scope"] == "CAMPAIGN" and not chunk.get("campaign_id"):
            report.errors.append(f"Campaign chunk {chunk['id']} lacks campaign_id")
        if chunk.get("metadata", {}).get("atomic") and chunk["token_count"] > config.chunking.atomic_max_tokens:
            report.warnings.append(f"Atomic chunk {chunk['id']} exceeds atomic_max_tokens")

    for mention in mentions.values():
        if mention["chunk_id"] not in chunks or mention["entity_id"] not in entities:
            report.errors.append(f"Mention {mention['id']} has a missing endpoint")

    for entity in entities.values():
        if entity.get("scope") == "CAMPAIGN" and not entity.get("campaign_id"):
            report.errors.append(f"Campaign entity {entity['id']} lacks campaign_id")

    for item in evidence.values():
        chunk = chunks.get(item["chunk_id"])
        if chunk is None:
            report.errors.append(f"Evidence {item['id']} references missing chunk")
        elif not quote_is_grounded(item["quote"], chunk["raw_text"]):
            report.errors.append(f"Evidence {item['id']} quote is not grounded")

    for assertion in assertions.values():
        subject = entities.get(assertion["subject_id"])
        if subject is None:
            report.errors.append(f"Assertion {assertion['id']} has missing subject")
        object_entity = entities.get(assertion.get("object_id"))
        if assertion.get("object_id") and object_entity is None:
            report.errors.append(f"Assertion {assertion['id']} has missing object")
        if assertion.get("canon_scope") == "CAMPAIGN" and not assertion.get("campaign_id"):
            report.errors.append(f"Campaign assertion {assertion['id']} lacks campaign_id")
        for endpoint in (subject, object_entity):
            if (
                endpoint is not None
                and endpoint.get("scope") == "CAMPAIGN"
                and endpoint.get("campaign_id") != assertion.get("campaign_id")
            ):
                report.errors.append(
                    f"Assertion {assertion['id']} crosses campaign identity scopes"
                )
        predicate = assertion["predicate_code"]
        if predicate not in schema.predicates:
            report.errors.append(f"Assertion {assertion['id']} has unknown predicate")
        elif subject is not None and object_entity is not None:
            rule = schema.predicates[predicate]
            if subject["entity_type"] not in rule["domain"]:
                report.errors.append(
                    f"Assertion {assertion['id']} subject type {subject['entity_type']} "
                    f"is outside {predicate} domain"
                )
            if object_entity["entity_type"] not in rule["range"]:
                report.errors.append(
                    f"Assertion {assertion['id']} object type {object_entity['entity_type']} "
                    f"is outside {predicate} range"
                )
        if bool(assertion.get("object_id")) == bool(assertion.get("literal_value")):
            report.errors.append(f"Assertion {assertion['id']} must have exactly one object form")
        for evidence_id in assertion.get("evidence_ids", []):
            if evidence_id not in evidence:
                report.errors.append(f"Assertion {assertion['id']} has missing evidence")
        if assertion.get("asserted_by_id") and assertion["asserted_by_id"] not in entities:
            report.errors.append(f"Assertion {assertion['id']} has missing asserted-by entity")
        for chunk_id in assertion.get("source_chunk_ids", []):
            if chunk_id not in chunks:
                report.errors.append(f"Assertion {assertion['id']} has missing provenance chunk")
            elif predicate in schema.predicates and predicate not in schema.allowed_predicates(
                chunks[chunk_id]["profile"]
            ):
                report.errors.append(
                    f"Assertion {assertion['id']} predicate {predicate} is outside "
                    f"chunk profile {chunks[chunk_id]['profile']}"
                )
        if assertion.get("derivation_method") in {"LLM_EXTRACTION", "DETERMINISTIC_PARSE"} and not assertion.get(
            "source_chunk_ids"
        ):
            report.errors.append(f"Assertion {assertion['id']} lacks provenance chunks")
    for embedding in embeddings.values():
        if embedding["chunk_id"] not in chunks:
            report.errors.append(f"Embedding {embedding['id']} references missing chunk")
        if len(embedding.get("vector", [])) != embedding.get("dimensions"):
            report.errors.append(f"Embedding {embedding['id']} dimension mismatch")
    return report


def write_audit_sample(
    config: LoaderConfig,
    output_path: str | Path,
    size: int = 25,
    seed: int = 42,
) -> int:
    store = RecordStore(config.workspace_dir)
    assertions = store.read("assertions")
    evidence = store.by_id("evidence")
    entities = store.by_id("entities")
    rng = random.Random(seed)
    sample = rng.sample(assertions, min(size, len(assertions)))
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "assertion_id",
                "subject",
                "predicate",
                "object",
                "truth_status",
                "confidence",
                "source_quote",
                "page_start",
                "page_end",
                "supported_yes_no",
                "review_notes",
            ],
        )
        writer.writeheader()
        for assertion in sample:
            first_evidence = next(
                (evidence[eid] for eid in assertion.get("evidence_ids", []) if eid in evidence),
                {},
            )
            writer.writerow(
                {
                    "assertion_id": assertion["id"],
                    "subject": entities.get(assertion["subject_id"], {}).get(
                        "canonical_name", assertion["subject_id"]
                    ),
                    "predicate": assertion["predicate_code"],
                    "object": entities.get(assertion.get("object_id"), {}).get(
                        "canonical_name", assertion.get("literal_value", "")
                    ),
                    "truth_status": assertion["truth_status"],
                    "confidence": assertion["confidence"],
                    "source_quote": first_evidence.get("quote", ""),
                    "page_start": first_evidence.get("page_start", ""),
                    "page_end": first_evidence.get("page_end", ""),
                    "supported_yes_no": "",
                    "review_notes": "",
                }
            )
    return len(sample)

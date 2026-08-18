from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .backbone import Backbone
from .chunking import build_chunks
from .config import LoaderConfig, load_manifest
from .costing import CostEstimate, select_within_budget
from .ids import normalize_text, sha256_bytes, sha256_file, stable_id
from .llm import (
    ExtractionPayload,
    OpenAIResponsesExtractor,
    batch_custom_id,
    prepare_openai_batch_requests,
    read_openai_batch_results,
)
from .mechanics import extract_structured_mechanics
from .models import (
    AssertionRecord,
    Chunk,
    EntityRecord,
    EvidenceRecord,
    MentionRecord,
    ReviewItem,
    SourceSpec,
    SourceRecord,
)
from .parsers import parse_document
from .resolution import EntityResolver
from .schema import SchemaRegistry
from .store import RecordStore
from .validation import validate_payload


INGEST_PIPELINE_VERSION = "loregraph-ingest-v3"


@dataclass(slots=True)
class IngestSummary:
    sources_seen: int = 0
    sources_parsed: int = 0
    sources_skipped_current: int = 0
    sources_no_index: int = 0
    blocks: int = 0
    chunks: int = 0
    deterministic_mentions: int = 0
    deterministic_entities: int = 0
    deterministic_assertions: int = 0
    structured_review_items: int = 0


@dataclass(slots=True)
class ExtractionSummary:
    attempted: int = 0
    accepted_payloads: int = 0
    quarantined_payloads: int = 0
    entities: int = 0
    assertions: int = 0
    review_items: int = 0


def ingest_manifest(config: LoaderConfig, manifest_path: str | Path) -> IngestSummary:
    store = RecordStore(config.workspace_dir)
    backbone = Backbone(config.backbone_path)
    schema = SchemaRegistry(config.schema_path)
    specs = load_manifest(manifest_path)
    summary = IngestSummary(sources_seen=len(specs))
    all_entity_rows = store.by_id("entities")
    all_mention_rows = store.by_id("mentions")
    all_evidence_rows = store.by_id("evidence")
    all_assertion_rows = store.by_id("assertions")
    all_review_rows = store.by_id("review_queue")
    all_embedding_rows = store.by_id("embeddings")
    all_block_rows = store.by_id("blocks")
    all_chunk_rows = store.by_id("chunks")
    source_rows = store.by_id("sources")

    for spec in specs:
        if spec.extraction_profile and spec.extraction_profile not in schema.profiles:
            raise ValueError(
                f"Source {spec.code} selects unknown extraction_profile "
                f"{spec.extraction_profile!r}"
            )
        path = Path(spec.path)
        file_hash = sha256_file(path)
        material_id = stable_id("global", "material", spec.code, spec.title)
        source_id = stable_id("global", "material-version", material_id, spec.edition, file_hash)
        parser_fingerprint = _fingerprint(
            {
                "file_hash": file_hash,
                "pipeline_version": INGEST_PIPELINE_VERSION,
                "backend": config.parsing.backend,
                "parser_packages": _parser_package_versions(),
                "ocr": config.parsing.ocr,
                "chunking": asdict(config.chunking),
                "backbone_version": backbone.version,
                "excludes": spec.excludes,
                "source_kind": spec.source_kind,
                "canon_scope": spec.canon_scope,
                "actuality": spec.actuality,
                "indexing_mode": spec.indexing_mode,
                "visibility": spec.visibility,
                "remote_processing_allowed": spec.remote_processing_allowed,
                "extraction_profile": spec.extraction_profile,
                "game_system": spec.game_system,
                "ruleset": spec.ruleset,
                "ruleset_version": spec.ruleset_version,
            }
        )

        for existing in source_rows.values():
            if existing["material_id"] == material_id:
                existing.setdefault("metadata", {})["is_current"] = existing["id"] == source_id

        if spec.indexing_mode in {"NO_INDEX", "METADATA_ONLY"}:
            old_chunk_ids = {
                row["id"] for row in all_chunk_rows.values() if row["source_id"] == source_id
            }
            _drop_chunk_derivatives(
                old_chunk_ids,
                all_entity_rows,
                all_mention_rows,
                all_evidence_rows,
                all_assertion_rows,
                all_review_rows,
                all_embedding_rows,
            )
            store.clear_stages(old_chunk_ids, ["extract", "embed"])
            all_block_rows = {
                key: row for key, row in all_block_rows.items() if row["source_id"] != source_id
            }
            all_chunk_rows = {
                key: row for key, row in all_chunk_rows.items() if row["source_id"] != source_id
            }
            record = _source_record(
                spec, source_id, material_id, file_hash, parser="not_parsed", page_count=None
            )
            record.metadata["is_current"] = True
            source_rows[source_id] = record.to_dict()
            store.mark_stage(source_id, "parse_chunk", parser_fingerprint, "SKIPPED_NO_INDEX")
            summary.sources_no_index += 1
            continue

        if store.stage_is_current(source_id, "parse_chunk", parser_fingerprint):
            summary.sources_skipped_current += 1
            continue

        parsed = parse_document(spec, source_id, config.parsing)
        record = _source_record(
            spec,
            source_id,
            material_id,
            file_hash,
            parser=parsed.parser_name,
            page_count=parsed.page_count,
        )
        record.metadata["is_current"] = True
        chunks = build_chunks(parsed.blocks, spec, source_id, config.chunking)
        old_chunk_ids = {
            row["id"] for row in all_chunk_rows.values() if row["source_id"] == source_id
        }
        _drop_chunk_derivatives(
            old_chunk_ids,
            all_entity_rows,
            all_mention_rows,
            all_evidence_rows,
            all_assertion_rows,
            all_review_rows,
            all_embedding_rows,
        )
        store.clear_stages(old_chunk_ids, ["extract", "embed"])
        all_block_rows = {
            key: row for key, row in all_block_rows.items() if row["source_id"] != source_id
        }
        all_chunk_rows = {
            key: row for key, row in all_chunk_rows.items() if row["source_id"] != source_id
        }
        all_block_rows.update({block.id: block.to_dict() for block in parsed.blocks})
        all_chunk_rows.update({chunk.id: chunk.to_dict() for chunk in chunks})
        source_rows[source_id] = record.to_dict()

        if spec.ruleset_version and chunks:
            declared_entities, declared_assertions = _manifest_rules_hierarchy(
                spec, chunks[0], source_id
            )
            for entity in declared_entities:
                all_entity_rows[entity.id] = _merge_entity_dicts(
                    all_entity_rows.get(entity.id), entity.to_dict()
                )
            for assertion in declared_assertions:
                all_assertion_rows[assertion.id] = assertion.to_dict()
            summary.deterministic_entities += len(declared_entities)
            summary.deterministic_assertions += len(declared_assertions)

        for chunk in chunks:
            entities, mentions = backbone.scan(chunk)
            for entity in entities:
                all_entity_rows[entity.id] = _merge_entity_dicts(
                    all_entity_rows.get(entity.id), entity.to_dict()
                )
            for mention in mentions:
                all_mention_rows[mention.id] = mention.to_dict()
            summary.deterministic_mentions += len(mentions)
            structured = extract_structured_mechanics(chunk, config.tenant_id)
            for entity in structured.entities:
                all_entity_rows[entity.id] = _merge_entity_dicts(
                    all_entity_rows.get(entity.id), entity.to_dict()
                )
            for evidence in structured.evidence:
                all_evidence_rows[evidence.id] = evidence.to_dict()
            for assertion in structured.assertions:
                all_assertion_rows[assertion.id] = assertion.to_dict()
            for review in structured.review_items:
                all_review_rows[review.id] = review.to_dict()
            summary.deterministic_entities += len(structured.entities)
            summary.deterministic_assertions += len(structured.assertions)
            summary.structured_review_items += len(structured.review_items)

        store.mark_stage(
            source_id,
            "parse_chunk",
            parser_fingerprint,
            "SUCCEEDED",
            {"parser": parsed.parser_name, "blocks": len(parsed.blocks), "chunks": len(chunks)},
        )
        summary.sources_parsed += 1
        summary.blocks += len(parsed.blocks)
        summary.chunks += len(chunks)

    store.replace("sources", source_rows.values())
    store.replace("blocks", all_block_rows.values())
    store.replace("chunks", all_chunk_rows.values())
    store.replace("entities", all_entity_rows.values())
    store.replace("mentions", all_mention_rows.values())
    store.replace("evidence", all_evidence_rows.values())
    store.replace("assertions", all_assertion_rows.values())
    store.replace("review_queue", all_review_rows.values())
    store.replace("embeddings", all_embedding_rows.values())
    return summary


def estimate_extraction(config: LoaderConfig, batch: bool = True) -> CostEstimate:
    store, schema, backbone = _components(config)
    chunks = [Chunk(**row) for row in store.read("chunks")]
    _, estimate = select_within_budget(chunks, schema, backbone, config.extraction, batch=batch)
    return estimate


def prepare_batch(
    config: LoaderConfig,
    output_path: str | Path,
) -> CostEstimate:
    if config.extraction.provider != "openai_batch":
        raise ValueError("Set extraction.provider='openai_batch' before prepare-batch")
    store, schema, backbone = _components(config)
    chunks = _pending_chunks(store, config, schema, backbone)
    selected, estimate = select_within_budget(
        chunks, schema, backbone, config.extraction, batch=True
    )
    prepare_openai_batch_requests(selected, schema, backbone, config.extraction, output_path)
    for chunk in selected:
        fingerprint = _extraction_fingerprint(chunk, config, schema)
        store.mark_stage(
            chunk.id,
            "extract",
            fingerprint,
            "BATCH_PREPARED",
            {"request_file": str(output_path), "model": config.extraction.model},
        )
    return estimate


def apply_batch(
    config: LoaderConfig,
    result_path: str | Path,
) -> ExtractionSummary:
    store, schema, backbone = _components(config)
    results = read_openai_batch_results(result_path)
    chunks = {row["id"]: Chunk(**row) for row in store.read("chunks")}
    by_batch_id = {batch_custom_id(chunk_id): chunk_id for chunk_id in chunks}
    unknown = set(results) - set(by_batch_id)
    if unknown:
        raise ValueError(
            "Batch results do not match the current chunk ledger; refusing partial remap: "
            f"{sorted(unknown)[:5]}"
        )
    remapped = {
        by_batch_id[custom_id]: payload
        for custom_id, payload in results.items()
        if custom_id in by_batch_id
    }
    return _apply_payloads(config, store, schema, backbone, chunks, remapped)


def extract_sync(config: LoaderConfig) -> tuple[ExtractionSummary, CostEstimate]:
    if config.extraction.provider != "openai":
        raise ValueError("Set extraction.provider='openai' before synchronous extract")
    store, schema, backbone = _components(config)
    chunks = _pending_chunks(store, config, schema, backbone)
    selected, estimate = select_within_budget(
        chunks, schema, backbone, config.extraction, batch=False
    )
    extractor = OpenAIResponsesExtractor(config.extraction)
    payloads: dict[str, ExtractionPayload] = {}
    for chunk in selected:
        payloads[chunk.id] = extractor.extract(chunk, schema, backbone)
        input_tokens, output_tokens = extractor.last_usage
        actual_cost = (
            input_tokens / 1_000_000 * config.extraction.input_price_per_million_usd
            + output_tokens / 1_000_000 * config.extraction.output_price_per_million_usd
        )
        store.record_usage(
            "openai",
            config.extraction.model,
            chunk.id,
            input_tokens,
            output_tokens,
            actual_cost,
        )
    chunk_index = {chunk.id: chunk for chunk in selected}
    summary = _apply_payloads(config, store, schema, backbone, chunk_index, payloads)
    return summary, estimate


def _apply_payloads(
    config: LoaderConfig,
    store: RecordStore,
    schema: SchemaRegistry,
    backbone: Backbone,
    chunks: dict[str, Chunk],
    payloads: dict[str, ExtractionPayload],
) -> ExtractionSummary:
    entity_rows = store.by_id("entities")
    entities = [EntityRecord(**row) for row in entity_rows.values()]
    for backbone_entity in backbone.entities.values():
        if backbone_entity.id not in entity_rows:
            entities.append(backbone_entity)
    resolver = EntityResolver(entities, config.resolution, config.tenant_id)
    assertion_rows = store.by_id("assertions")
    evidence_rows = store.by_id("evidence")
    mention_rows = store.by_id("mentions")
    review_rows = store.by_id("review_queue")
    summary = ExtractionSummary()

    for chunk_id, payload in payloads.items():
        chunk = chunks.get(chunk_id)
        if chunk is None:
            continue
        summary.attempted += 1
        errors = validate_payload(payload, chunk, schema)
        fingerprint = _extraction_fingerprint(chunk, config, schema)
        if errors:
            review = ReviewItem(
                id=stable_id("global", "review", chunk.id, "payload-validation"),
                item_type="EXTRACTION_PAYLOAD",
                reason="; ".join(errors),
                payload=payload.model_dump(),
                source_chunk_id=chunk.id,
            )
            review_rows[review.id] = review.to_dict()
            store.mark_stage(
                chunk.id, "extract", fingerprint, "QUARANTINED", {"errors": errors}
            )
            summary.quarantined_payloads += 1
            summary.review_items += 1
            continue

        resolved: dict[str, EntityRecord] = {}
        for candidate in payload.nodes:
            result = resolver.resolve(candidate, chunk)
            resolved[candidate.temp_id] = result.entity
            entity_rows[result.entity.id] = _merge_entity_dicts(
                entity_rows.get(result.entity.id), result.entity.to_dict()
            )
            if result.review_item:
                review_rows[result.review_item.id] = result.review_item.to_dict()
                summary.review_items += 1
            mention = _llm_mention(candidate.name, result.entity.id, chunk)
            if mention:
                mention_rows[mention.id] = mention.to_dict()

        for relationship in payload.relationships:
            evidence = _evidence(chunk, relationship.source_quote)
            assertion = _assertion(
                chunk=chunk,
                subject_id=resolved[relationship.subject].id,
                predicate=relationship.predicate,
                object_id=resolved[relationship.object].id,
                literal_value=None,
                quote=relationship.source_quote,
                confidence=relationship.confidence,
                truth_status="ASSERTED",
                evidence_id=evidence.id,
                valid_from=relationship.valid_from,
                valid_to=relationship.valid_to,
            )
            evidence_rows[evidence.id] = evidence.to_dict()
            assertion_rows[assertion.id] = assertion.to_dict()
            _queue_low_confidence(assertion, chunk, review_rows, config, summary)

        for claim in payload.claims:
            evidence = _evidence(chunk, claim.source_quote)
            assertion = _assertion(
                chunk=chunk,
                subject_id=resolved[claim.subject].id,
                predicate=claim.predicate,
                object_id=resolved[claim.object].id if claim.object else None,
                literal_value=claim.literal_value,
                quote=claim.source_quote,
                confidence=claim.confidence,
                truth_status=claim.truth_status,
                evidence_id=evidence.id,
                valid_from=claim.valid_from,
                valid_to=claim.valid_to,
                asserted_by_id=resolved[claim.asserted_by].id
                if claim.asserted_by in resolved
                else None,
            )
            evidence_rows[evidence.id] = evidence.to_dict()
            assertion_rows[assertion.id] = assertion.to_dict()
            _queue_low_confidence(assertion, chunk, review_rows, config, summary)

        summary.accepted_payloads += 1
        summary.entities += len(payload.nodes)
        summary.assertions += len(payload.relationships) + len(payload.claims)
        store.mark_stage(
            chunk.id,
            "extract",
            fingerprint,
            "SUCCEEDED",
            {
                "model": config.extraction.model,
                "nodes": len(payload.nodes),
                "assertions": len(payload.relationships) + len(payload.claims),
            },
        )

    store.replace("entities", entity_rows.values())
    store.replace("mentions", mention_rows.values())
    store.replace("evidence", evidence_rows.values())
    store.replace("assertions", assertion_rows.values())
    store.replace("review_queue", review_rows.values())
    return summary


def _components(config: LoaderConfig) -> tuple[RecordStore, SchemaRegistry, Backbone]:
    return RecordStore(config.workspace_dir), SchemaRegistry(config.schema_path), Backbone(
        config.backbone_path
    )


def _pending_chunks(
    store: RecordStore,
    config: LoaderConfig,
    schema: SchemaRegistry,
    backbone: Backbone,
) -> list[Chunk]:
    chunks = [Chunk(**row) for row in store.read("chunks")]
    if config.extraction.mode != "changed":
        return chunks
    return [
        chunk
        for chunk in chunks
        if not store.stage_is_current(
            chunk.id, "extract", _extraction_fingerprint(chunk, config, schema)
        )
    ]


def _source_record(
    spec: SourceSpec,
    source_id: str,
    material_id: str,
    file_hash: str,
    parser: str,
    page_count: int | None,
) -> SourceRecord:
    return SourceRecord(
        id=source_id,
        material_id=material_id,
        path=spec.path,
        code=spec.code,
        title=spec.title,
        edition=spec.edition,
        source_kind=spec.source_kind,
        canon_scope=spec.canon_scope,
        actuality=spec.actuality,
        rights_basis=spec.rights_basis,
        indexing_mode=spec.indexing_mode,
        visibility=spec.visibility,
        attribution=spec.attribution,
        remote_processing_allowed=spec.remote_processing_allowed,
        game_system=spec.game_system,
        ruleset=spec.ruleset,
        ruleset_version=spec.ruleset_version,
        file_hash=file_hash,
        parser=parser,
        campaign_id=spec.campaign_id,
        page_count=page_count,
        metadata={"extraction_profile": spec.extraction_profile},
    )


def _manifest_rules_hierarchy(
    spec: SourceSpec,
    chunk: Chunk,
    source_id: str,
) -> tuple[list[EntityRecord], list[AssertionRecord]]:
    values = [
        ("GameSystem", "game-system", spec.game_system),
        ("Ruleset", "ruleset", spec.ruleset),
        ("RulesetVersion", "ruleset-version", spec.ruleset_version),
    ]
    entities = [
        EntityRecord(
            id=stable_id("global", key, normalize_text(name).casefold()),
            entity_type=entity_type,
            subtype="other",
            canonical_name=name,
            aliases=[],
            scope="GLOBAL",
            confidence=1.0,
            provisional=False,
            source_chunk_ids=[chunk.id],
            metadata={"declared_by_manifest": True, "source_id": source_id},
        )
        for entity_type, key, name in values
        if name
    ]
    by_type = {entity.entity_type: entity for entity in entities}
    assertions: list[AssertionRecord] = []
    if chunk.profile == "rules" and {"GameSystem", "Ruleset", "RulesetVersion"} <= by_type.keys():
        for subject_type, predicate, object_type in (
            ("GameSystem", "HAS_RULESET", "Ruleset"),
            ("Ruleset", "HAS_RULESET_VERSION", "RulesetVersion"),
        ):
            subject = by_type[subject_type]
            object_entity = by_type[object_type]
            assertions.append(
                AssertionRecord(
                    id=stable_id(
                        "global", "assertion", subject.id, predicate, object_entity.id, source_id
                    ),
                    subject_id=subject.id,
                    predicate_code=predicate,
                    object_id=object_entity.id,
                    literal_value=None,
                    truth_status="ASSERTED",
                    canon_scope="NONE",
                    actuality="AUTHORED",
                    confidence=1.0,
                    review_status="ACCEPTED",
                    derivation_method="MANIFEST_DECLARATION",
                    source_chunk_ids=[chunk.id],
                    evidence_ids=[],
                    visibility=chunk.visibility,
                    metadata={"declared_by_manifest": True, "source_id": source_id},
                )
            )
    return entities, assertions


def _drop_chunk_derivatives(
    chunk_ids: set[str],
    entities: dict[str, dict[str, Any]],
    mentions: dict[str, dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    assertions: dict[str, dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
    embeddings: dict[str, dict[str, Any]],
) -> None:
    if not chunk_ids:
        return
    for table in (mentions, evidence, embeddings):
        for key in [key for key, row in table.items() if row.get("chunk_id") in chunk_ids]:
            del table[key]
    for key in [
        key
        for key, row in assertions.items()
        if chunk_ids.intersection(row.get("source_chunk_ids", []))
    ]:
        del assertions[key]
    for key in [
        key for key, row in reviews.items() if row.get("source_chunk_id") in chunk_ids
    ]:
        del reviews[key]

    referenced = {
        endpoint
        for row in assertions.values()
        for endpoint in (row.get("subject_id"), row.get("object_id"), row.get("asserted_by_id"))
        if endpoint
    }
    referenced.update(row["entity_id"] for row in mentions.values())
    for key, entity in list(entities.items()):
        entity["source_chunk_ids"] = [
            value for value in entity.get("source_chunk_ids", []) if value not in chunk_ids
        ]
        derived = entity.get("provisional") or entity.get("metadata", {}).get(
            "deterministic_parser"
        ) or entity.get("metadata", {}).get("backbone_version") or entity.get(
            "metadata", {}
        ).get("declared_by_manifest")
        if derived and not entity["source_chunk_ids"] and key not in referenced:
            del entities[key]


def _evidence(chunk: Chunk, quote: str) -> EvidenceRecord:
    normalized = normalize_text(quote)
    return EvidenceRecord(
        id=stable_id("global", "fragment", chunk.id, normalized),
        chunk_id=chunk.id,
        source_id=chunk.source_id,
        quote=quote,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        exact_text_hash=sha256_bytes(normalized.encode("utf-8")),
    )


def _assertion(
    *,
    chunk: Chunk,
    subject_id: str,
    predicate: str,
    object_id: str | None,
    literal_value: str | None,
    quote: str,
    confidence: float,
    truth_status: str,
    evidence_id: str,
    valid_from: str | None,
    valid_to: str | None,
    asserted_by_id: str | None = None,
) -> AssertionRecord:
    return AssertionRecord(
        id=stable_id(
            "global",
            "assertion",
            chunk.canon_scope,
            chunk.campaign_id or "global",
            chunk.id,
            subject_id,
            predicate,
            object_id or literal_value or "",
            normalize_text(quote),
        ),
        subject_id=subject_id,
        predicate_code=predicate,
        object_id=object_id,
        literal_value=literal_value,
        truth_status=truth_status,
        canon_scope=chunk.canon_scope,
        actuality=chunk.actuality,
        confidence=confidence,
        review_status="UNREVIEWED",
        derivation_method="LLM_EXTRACTION",
        source_chunk_ids=[chunk.id],
        evidence_ids=[evidence_id],
        campaign_id=chunk.campaign_id,
        asserted_by_id=asserted_by_id,
        valid_from=valid_from,
        valid_to=valid_to,
        visibility=chunk.visibility,
        metadata={"source_quote": quote},
    )


def _queue_low_confidence(
    assertion: AssertionRecord,
    chunk: Chunk,
    review_rows: dict[str, dict[str, Any]],
    config: LoaderConfig,
    summary: ExtractionSummary,
) -> None:
    if assertion.confidence >= config.extraction.confidence_threshold:
        return
    assertion.review_status = "NEEDS_REVIEW"
    review = ReviewItem(
        id=stable_id("global", "review", assertion.id, "low-confidence"),
        item_type="ASSERTION",
        reason=f"Confidence {assertion.confidence:.3f} below threshold",
        payload=assertion.to_dict(),
        source_chunk_id=chunk.id,
    )
    review_rows[review.id] = review.to_dict()
    summary.review_items += 1


def _llm_mention(name: str, entity_id: str, chunk: Chunk) -> MentionRecord | None:
    start = chunk.raw_text.casefold().find(name.casefold())
    if start < 0:
        return None
    end = start + len(name)
    return MentionRecord(
        id=stable_id("global", "mention", chunk.id, entity_id, start, end),
        chunk_id=chunk.id,
        entity_id=entity_id,
        surface=chunk.raw_text[start:end],
        start=start,
        end=end,
        method="LLM_EXTRACTED_NODE",
        confidence=0.8,
    )


def _merge_entity_dicts(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    if existing is None:
        return incoming
    result = dict(existing)
    result["aliases"] = sorted(set(existing.get("aliases", [])) | set(incoming.get("aliases", [])))
    result["source_chunk_ids"] = sorted(
        set(existing.get("source_chunk_ids", [])) | set(incoming.get("source_chunk_ids", []))
    )
    result["confidence"] = max(existing.get("confidence", 0), incoming.get("confidence", 0))
    result["provisional"] = existing.get("provisional", True) and incoming.get(
        "provisional", True
    )
    result["metadata"] = {**existing.get("metadata", {}), **incoming.get("metadata", {})}
    return result


def _extraction_fingerprint(
    chunk: Chunk,
    config: LoaderConfig,
    schema: SchemaRegistry,
) -> str:
    return _fingerprint(
        {
            "chunk_hash": chunk.text_hash,
            "model": config.extraction.model,
            "prompt_version": config.extraction.prompt_version,
            "schema_version": schema.data["schema_version"],
            "profile": chunk.profile,
        }
    )


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(canonical.encode("utf-8"))


def _parser_package_versions() -> dict[str, str]:
    output: dict[str, str] = {}
    for distribution in ("docling", "PyMuPDF"):
        try:
            output[distribution] = version(distribution)
        except PackageNotFoundError:
            output[distribution] = "not-installed"
    return output

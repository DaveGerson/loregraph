from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SourceSpec:
    path: str
    code: str
    title: str
    edition: str
    source_kind: str
    canon_scope: str
    actuality: str
    rights_basis: str
    indexing_mode: str
    visibility: str = "DM_ONLY"
    attribution: str = ""
    remote_processing_allowed: bool = False
    extraction_profile: str | None = None
    game_system: str | None = None
    ruleset: str | None = None
    ruleset_version: str | None = None
    campaign_id: str | None = None
    excludes: list[list[int]] = field(default_factory=list)


@dataclass(slots=True)
class SourceRecord:
    id: str
    material_id: str
    path: str
    code: str
    title: str
    edition: str
    source_kind: str
    canon_scope: str
    actuality: str
    rights_basis: str
    indexing_mode: str
    visibility: str
    attribution: str
    remote_processing_allowed: bool
    game_system: str | None
    ruleset: str | None
    ruleset_version: str | None
    file_hash: str
    parser: str
    campaign_id: str | None = None
    page_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Block:
    id: str
    source_id: str
    order: int
    page_start: int
    page_end: int
    text: str
    heading_path: list[str]
    content_type: str
    atomic: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Chunk:
    id: str
    source_id: str
    chunk_index: int
    text: str
    raw_text: str
    text_hash: str
    token_count: int
    page_start: int
    page_end: int
    heading_path: list[str]
    content_type: str
    profile: str
    actuality: str
    canon_scope: str
    visibility: str
    indexing_mode: str
    extraction_eligible: bool
    remote_processing_allowed: bool = False
    campaign_id: str | None = None
    game_system: str | None = None
    ruleset: str | None = None
    ruleset_version: str | None = None
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EntityRecord:
    id: str
    entity_type: str
    subtype: str
    canonical_name: str
    aliases: list[str]
    scope: str
    campaign_id: str | None = None
    description: str = ""
    confidence: float = 1.0
    provisional: bool = False
    source_chunk_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MentionRecord:
    id: str
    chunk_id: str
    entity_id: str
    surface: str
    start: int
    end: int
    method: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EvidenceRecord:
    id: str
    chunk_id: str
    source_id: str
    quote: str
    page_start: int
    page_end: int
    exact_text_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AssertionRecord:
    id: str
    subject_id: str
    predicate_code: str
    object_id: str | None
    literal_value: str | None
    truth_status: str
    canon_scope: str
    actuality: str
    confidence: float
    review_status: str
    derivation_method: str
    source_chunk_ids: list[str]
    evidence_ids: list[str]
    campaign_id: str | None = None
    asserted_by_id: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    visibility: str = "DM_ONLY"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReviewItem:
    id: str
    item_type: str
    reason: str
    payload: dict[str, Any]
    source_chunk_id: str | None = None
    status: str = "OPEN"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EmbeddingRecord:
    id: str
    chunk_id: str
    provider: str
    model: str
    dimensions: int
    input_hash: str
    vector: list[float]
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

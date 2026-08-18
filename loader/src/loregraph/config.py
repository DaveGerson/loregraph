from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import SourceSpec


@dataclass(slots=True)
class ParsingConfig:
    backend: str = "auto"
    ocr: bool = False
    remove_repeated_margins: bool = True


@dataclass(slots=True)
class ChunkingConfig:
    target_tokens: int = 700
    max_tokens: int = 950
    min_tokens: int = 80
    atomic_max_tokens: int = 2600
    overlap_tokens: int = 0


@dataclass(slots=True)
class ExtractionConfig:
    provider: str = "none"
    model: str = "gpt-5-mini"
    prompt_version: str = "loregraph-v2"
    mode: str = "changed"
    confidence_threshold: float = 0.70
    max_chunks: int = 500
    max_input_tokens: int = 400_000
    max_output_tokens_per_chunk: int = 1200
    max_estimated_cost_usd: float = 15.0
    input_price_per_million_usd: float = 0.0
    output_price_per_million_usd: float = 0.0
    batch_price_multiplier: float = 0.5
    content_types: list[str] = field(
        default_factory=lambda: ["lore", "adventure", "rules", "item"]
    )


@dataclass(slots=True)
class EmbeddingConfig:
    provider: str = "none"
    model: str = "Alibaba-NLP/gte-modernbert-base"
    dimensions: int = 0
    batch_size: int = 32
    max_input_tokens: int = 5_000_000
    vector_index_name: str = "chunk_embedding"


@dataclass(slots=True)
class ResolutionConfig:
    exact_alias: bool = True
    fuzzy_threshold: float = 0.94
    review_threshold: float = 0.84


@dataclass(slots=True)
class Neo4jConfig:
    enabled: bool = False
    database: str = "neo4j"
    batch_size: int = 500
    create_projection_edges: bool = True
    project_unreviewed: bool = True


@dataclass(slots=True)
class ObsidianConfig:
    enabled: bool = True
    vault_path: str = "output/LoreGraph"
    write_chunk_notes: bool = False
    write_assertion_notes: bool = True
    include_superseded: bool = False
    max_quote_chars: int = 500


@dataclass(slots=True)
class LoaderConfig:
    tenant_id: str = "personal"
    workspace_dir: Path = Path(".loregraph")
    schema_path: Path = Path("schema/canonical-profile.json")
    backbone_path: Path = Path("config/backbone.empty.json")
    parsing: ParsingConfig = field(default_factory=ParsingConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    resolution: ResolutionConfig = field(default_factory=ResolutionConfig)
    neo4j: Neo4jConfig = field(default_factory=Neo4jConfig)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    config_dir: Path = Path(".")

    @classmethod
    def from_toml(cls, path: str | Path) -> "LoaderConfig":
        config_path = Path(path).resolve()
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        project = raw.get("project", {})
        base = config_path.parent

        def resolve(value: str) -> Path:
            candidate = Path(value)
            return candidate if candidate.is_absolute() else (base / candidate).resolve()

        obj = cls(
            tenant_id=str(project.get("tenant_id", "personal")),
            workspace_dir=resolve(project.get("workspace_dir", ".loregraph")),
            schema_path=resolve(project.get("schema_path", "schema/canonical-profile.json")),
            backbone_path=resolve(project.get("backbone_path", "config/backbone.empty.json")),
            parsing=_section(ParsingConfig, raw.get("parsing", {})),
            chunking=_section(ChunkingConfig, raw.get("chunking", {})),
            extraction=_section(ExtractionConfig, raw.get("extraction", {})),
            embeddings=_section(EmbeddingConfig, raw.get("embeddings", {})),
            resolution=_section(ResolutionConfig, raw.get("resolution", {})),
            neo4j=_section(Neo4jConfig, raw.get("neo4j", {})),
            obsidian=_section(ObsidianConfig, raw.get("obsidian", {})),
            config_dir=base,
        )
        vault = Path(obj.obsidian.vault_path)
        if not vault.is_absolute():
            obj.obsidian.vault_path = str((base / vault).resolve())
        obj.validate()
        return obj

    def validate(self) -> None:
        if self.chunking.overlap_tokens != 0:
            raise ValueError("Extraction overlap must remain zero; use graph neighbor expansion instead.")
        if self.chunking.min_tokens > self.chunking.target_tokens:
            raise ValueError("chunking.min_tokens cannot exceed target_tokens")
        if self.chunking.target_tokens > self.chunking.max_tokens:
            raise ValueError("chunking.target_tokens cannot exceed max_tokens")
        if self.extraction.provider not in {"none", "openai", "openai_batch"}:
            raise ValueError("extraction.provider must be none, openai, or openai_batch")
        if not 0 <= self.extraction.confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be between zero and one")
        if self.embeddings.provider not in {"none", "sentence_transformers", "openai"}:
            raise ValueError("embeddings.provider must be none, sentence_transformers, or openai")
        if not self.embeddings.vector_index_name.replace("_", "").isalnum():
            raise ValueError("embeddings.vector_index_name must be alphanumeric/underscore")

    @property
    def neo4j_credentials(self) -> dict[str, str]:
        return {
            "uri": os.getenv("NEO4J_URI", ""),
            "username": os.getenv("NEO4J_USERNAME", "neo4j"),
            "password": os.getenv("NEO4J_PASSWORD", ""),
            "database": os.getenv("NEO4J_DATABASE", self.neo4j.database),
        }


def load_manifest(path: str | Path) -> list[SourceSpec]:
    manifest_path = Path(path).resolve()
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    rows = payload.get("sources", payload.get("books", []))
    if not isinstance(rows, list):
        raise ValueError("The manifest must contain a top-level sources list")
    specs: list[SourceSpec] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each sources entry must be a mapping")
        source = SourceSpec(**row)
        candidate = Path(source.path)
        if not candidate.is_absolute():
            source.path = str((manifest_path.parent / candidate).resolve())
        if source.indexing_mode == "UNKNOWN":
            source.indexing_mode = "NO_INDEX"
        _validate_source_spec(source)
        identity = (source.code, source.edition)
        if identity in seen:
            raise ValueError(f"Duplicate source code/edition in manifest: {identity}")
        seen.add(identity)
        specs.append(source)
    return specs


def _validate_source_spec(source: SourceSpec) -> None:
    allowed_source_kinds = {
        "setting", "adventure", "rules", "campaign", "fiction", "kanon", "homebrew",
        "note", "document", "web", "other"
    }
    allowed_actuality = {"AUTHORED", "PLANNED", "OCCURRED", "HYPOTHETICAL"}
    allowed_scopes = {
        "SETTING_SOURCE", "ADVENTURE_SOURCE", "CAMPAIGN", "HOMEBREW",
        "REFERENCE", "PERSONAL", "NONE"
    }
    allowed_indexing = {
        "NO_INDEX", "METADATA_ONLY", "PRIVATE_ONLY", "FULL_TEXT", "FULL_TEXT_WITH_ATTRIBUTION"
    }
    allowed_visibility = {"PUBLIC", "PLAYER", "DM_ONLY", "PRIVATE"}
    if source.source_kind not in allowed_source_kinds:
        raise ValueError(f"Unknown source_kind {source.source_kind!r}")
    if source.actuality not in allowed_actuality:
        raise ValueError(f"Unknown actuality {source.actuality!r}")
    if source.canon_scope not in allowed_scopes:
        raise ValueError(f"Unknown canon_scope {source.canon_scope!r}")
    if source.indexing_mode not in allowed_indexing:
        raise ValueError(f"Unknown indexing_mode {source.indexing_mode!r}")
    if source.visibility not in allowed_visibility:
        raise ValueError(f"Unknown visibility {source.visibility!r}")
    if source.source_kind == "adventure" and source.canon_scope != "ADVENTURE_SOURCE":
        raise ValueError("Published adventures must use canon_scope=ADVENTURE_SOURCE")
    if source.source_kind == "campaign" and source.canon_scope != "CAMPAIGN":
        raise ValueError("Campaign inputs must use canon_scope=CAMPAIGN")
    if source.source_kind == "campaign" and not source.campaign_id:
        raise ValueError("Campaign inputs require campaign_id")
    if source.source_kind == "rules" and source.canon_scope != "NONE":
        raise ValueError("Rules inputs must use canon_scope=NONE")
    if source.source_kind == "rules" and not source.ruleset_version:
        raise ValueError("Rules inputs require ruleset_version")
    if bool(source.game_system) != bool(source.ruleset):
        raise ValueError("game_system and ruleset must be declared together")
    if source.indexing_mode == "FULL_TEXT_WITH_ATTRIBUTION" and not source.attribution.strip():
        raise ValueError("FULL_TEXT_WITH_ATTRIBUTION requires attribution text")


def _section(kind: type[Any], values: dict[str, Any]) -> Any:
    allowed = kind.__dataclass_fields__.keys()
    return kind(**{key: value for key, value in values.items() if key in allowed})

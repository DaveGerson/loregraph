from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from .config import ResolutionConfig
from .ids import normalize_name, stable_id
from .llm import ExtractedNode
from .models import Chunk, EntityRecord, ReviewItem


RECORD_LIKE_TYPES = {
    "CharacterBuild",
    "CharacterState",
    "ItemInstance",
    "CampaignRevision",
    "SceneRun",
    "EncounterRun",
}


@dataclass(slots=True)
class ResolutionResult:
    entity: EntityRecord
    review_item: ReviewItem | None = None
    method: str = "NEW"


class EntityResolver:
    def __init__(
        self,
        entities: list[EntityRecord],
        config: ResolutionConfig,
        tenant_id: str,
    ):
        self.config = config
        self.tenant_id = tenant_id
        self.entities: dict[str, EntityRecord] = {entity.id: entity for entity in entities}

    def resolve(self, candidate: ExtractedNode, chunk: Chunk) -> ResolutionResult:
        if candidate.match_id and candidate.match_id in self.entities:
            matched = self.entities[candidate.match_id]
            if matched.entity_type == candidate.entity_type:
                return ResolutionResult(self._with_source(matched, chunk.id), method="GAZETTEER_ID")

        exact = self._exact_candidates(candidate, chunk)
        if len(exact) == 1:
            if candidate.entity_type in RECORD_LIKE_TYPES and chunk.id not in exact[0].source_chunk_ids:
                review = self._review(
                    candidate,
                    chunk,
                    "Record-like entity has the same name in another source chunk",
                    exact,
                )
                entity = self._new_entity(candidate, chunk, provisional=True)
                self.entities[entity.id] = entity
                return ResolutionResult(entity, review, "INSTANCE_REVIEW")
            return ResolutionResult(self._with_source(exact[0], chunk.id), method="EXACT_ALIAS")
        if len(exact) > 1:
            review = self._review(candidate, chunk, "Ambiguous exact alias", exact)
            return ResolutionResult(self._new_entity(candidate, chunk, provisional=True), review, "AMBIGUOUS")

        scored = self._fuzzy_candidates(candidate, chunk)
        if scored:
            best_score, best = scored[0]
            margin = best_score - scored[1][0] if len(scored) > 1 else 1.0
            if best_score >= self.config.fuzzy_threshold and margin >= 0.03:
                return ResolutionResult(self._with_source(best, chunk.id), method="FUZZY_HIGH")
            if best_score >= self.config.review_threshold:
                review = self._review(candidate, chunk, f"Fuzzy match {best_score:.3f}", [best])
                return ResolutionResult(self._new_entity(candidate, chunk, provisional=True), review, "FUZZY_REVIEW")

        entity = self._new_entity(candidate, chunk, provisional=True)
        self.entities[entity.id] = entity
        return ResolutionResult(entity, method="NEW_PROVISIONAL")

    def _exact_candidates(self, candidate: ExtractedNode, chunk: Chunk) -> list[EntityRecord]:
        surface_keys = {normalize_name(candidate.name), *(normalize_name(a) for a in candidate.aliases)}
        matches = []
        for entity in self.entities.values():
            if entity.entity_type != candidate.entity_type:
                continue
            if (
                entity.scope == "CAMPAIGN"
                and entity.campaign_id != chunk.campaign_id
            ):
                continue
            known = {
                normalize_name(entity.canonical_name),
                *(normalize_name(alias) for alias in entity.aliases),
            }
            if surface_keys & known:
                matches.append(entity)
        return matches

    def _fuzzy_candidates(
        self, candidate: ExtractedNode, chunk: Chunk
    ) -> list[tuple[float, EntityRecord]]:
        if candidate.entity_type in RECORD_LIKE_TYPES:
            return []
        needle = normalize_name(candidate.name)
        scored: list[tuple[float, EntityRecord]] = []
        for entity in self.entities.values():
            if entity.entity_type != candidate.entity_type:
                continue
            if entity.scope == "CAMPAIGN" and entity.campaign_id != chunk.campaign_id:
                continue
            surfaces = [entity.canonical_name, *entity.aliases]
            score = max(
                SequenceMatcher(None, needle, normalize_name(surface)).ratio() for surface in surfaces
            )
            if score >= self.config.review_threshold:
                scored.append((score, entity))
        return sorted(scored, key=lambda pair: pair[0], reverse=True)

    def _new_entity(self, candidate: ExtractedNode, chunk: Chunk, provisional: bool) -> EntityRecord:
        scope = "CAMPAIGN" if chunk.canon_scope == "CAMPAIGN" else "GLOBAL"
        record_like = candidate.entity_type in RECORD_LIKE_TYPES
        entity_id = stable_id(
            self.tenant_id if scope == "CAMPAIGN" else "global",
            candidate.entity_type.casefold(),
            chunk.campaign_id or "global",
            normalize_name(candidate.name),
            candidate.subtype,
            chunk.id if record_like else "identity",
        )
        return EntityRecord(
            id=entity_id,
            entity_type=candidate.entity_type,
            subtype=candidate.subtype or "other",
            canonical_name=candidate.name,
            aliases=sorted(set(candidate.aliases)),
            scope=scope,
            campaign_id=chunk.campaign_id if scope == "CAMPAIGN" else None,
            confidence=candidate.confidence,
            provisional=provisional,
            source_chunk_ids=[chunk.id],
            metadata={
                "extraction_temp_id": candidate.temp_id,
                "structured": {item.name: item.value for item in candidate.properties},
                "structured_evidence": {
                    item.name: item.source_quote for item in candidate.properties
                },
            },
        )

    @staticmethod
    def _with_source(entity: EntityRecord, chunk_id: str) -> EntityRecord:
        copy = EntityRecord(**entity.to_dict())
        copy.source_chunk_ids = sorted(set([*copy.source_chunk_ids, chunk_id]))
        return copy

    def _review(
        self,
        candidate: ExtractedNode,
        chunk: Chunk,
        reason: str,
        matches: list[EntityRecord],
    ) -> ReviewItem:
        return ReviewItem(
            id=stable_id("global", "review", chunk.id, candidate.temp_id, reason),
            item_type="ENTITY_RESOLUTION",
            reason=reason,
            source_chunk_id=chunk.id,
            payload={
                "candidate": candidate.model_dump(),
                "possible_matches": [match.to_dict() for match in matches],
            },
        )

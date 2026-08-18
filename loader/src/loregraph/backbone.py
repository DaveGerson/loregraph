from __future__ import annotations

import json
import re
from pathlib import Path

from .ids import normalize_name, stable_id
from .models import Chunk, EntityRecord, MentionRecord


class Backbone:
    def __init__(self, path: str | Path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.version = payload.get("version", "unknown")
        self.entities: dict[str, EntityRecord] = {}
        self.alias_index: dict[tuple[str, str], set[str]] = {}
        for raw in payload.get("entities", []):
            entity = EntityRecord(
                id=raw["id"],
                entity_type=raw["type"],
                subtype=raw.get("subtype", "other"),
                canonical_name=raw["name"],
                aliases=list(raw.get("aliases", [])),
                scope="GLOBAL",
                confidence=1.0,
                provisional=False,
                metadata={"backbone_version": self.version},
            )
            self.entities[entity.id] = entity
            for surface in [entity.canonical_name, *entity.aliases]:
                key = (entity.entity_type, normalize_name(surface))
                self.alias_index.setdefault(key, set()).add(entity.id)

    def scan(self, chunk: Chunk) -> tuple[list[EntityRecord], list[MentionRecord]]:
        found_entities: dict[str, EntityRecord] = {}
        mentions: list[MentionRecord] = []
        occupied: list[tuple[int, int]] = []
        surfaces: list[tuple[str, str]] = []
        for entity in self.entities.values():
            for surface in [entity.canonical_name, *entity.aliases]:
                if len(surface.strip()) >= 3:
                    surfaces.append((surface, entity.id))
        for surface, entity_id in sorted(surfaces, key=lambda item: len(item[0]), reverse=True):
            pattern = re.compile(rf"(?<![\w']){re.escape(surface)}(?![\w'])", re.IGNORECASE)
            for match in pattern.finditer(chunk.raw_text):
                span = (match.start(), match.end())
                if any(span[0] < end and start < span[1] for start, end in occupied):
                    continue
                occupied.append(span)
                original = self.entities[entity_id]
                entity = EntityRecord(**original.to_dict())
                entity.source_chunk_ids = sorted(set([*entity.source_chunk_ids, chunk.id]))
                found_entities[entity_id] = entity
                mentions.append(
                    MentionRecord(
                        id=stable_id("global", "mention", chunk.id, entity_id, *span),
                        chunk_id=chunk.id,
                        entity_id=entity_id,
                        surface=match.group(0),
                        start=span[0],
                        end=span[1],
                        method="BACKBONE_ALIAS",
                        confidence=1.0,
                    )
                )
        return list(found_entities.values()), mentions

    def candidates_for_text(self, text: str, limit: int = 25) -> list[dict[str, object]]:
        matches: list[dict[str, object]] = []
        for entity in self.entities.values():
            surfaces = [entity.canonical_name, *entity.aliases]
            if any(
                re.search(
                    rf"(?<![\w']){re.escape(surface)}(?![\w'])",
                    text,
                    re.IGNORECASE,
                )
                for surface in surfaces
            ):
                matches.append(
                    {
                        "id": entity.id,
                        "type": entity.entity_type,
                        "subtype": entity.subtype,
                        "name": entity.canonical_name,
                        "aliases": entity.aliases,
                    }
                )
        return matches[:limit]

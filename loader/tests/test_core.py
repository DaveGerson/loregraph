from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loregraph.backbone import Backbone
from loregraph.chunking import build_chunks
from loregraph.config import ChunkingConfig, ParsingConfig, ResolutionConfig
from loregraph.llm import (
    ExtractedNode,
    ExtractedRelationship,
    ExtractionPayload,
    prepare_openai_batch_requests,
    strict_json_schema,
)
from loregraph.mechanics import extract_structured_mechanics
from loregraph.models import Block, Chunk, EntityRecord, SourceSpec
from loregraph.parsers import parse_document
from loregraph.resolution import EntityResolver
from loregraph.schema import SchemaRegistry
from loregraph.validation import quote_is_grounded, validate_payload


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "canonical-profile.json"
BACKBONE = ROOT / "config" / "backbone.eberron.json"


def source_spec(source_kind: str = "setting") -> SourceSpec:
    return SourceSpec(
        path="sample.md",
        code="SAMPLE",
        title="Sample",
        edition="test",
        source_kind=source_kind,
        canon_scope="SETTING_SOURCE" if source_kind != "campaign" else "CAMPAIGN",
        actuality="AUTHORED" if source_kind != "campaign" else "OCCURRED",
        rights_basis="USER_OWNED",
        indexing_mode="FULL_TEXT",
        campaign_id="campaign:test" if source_kind == "campaign" else None,
    )


def chunk(raw_text: str, profile: str = "lore") -> Chunk:
    return Chunk(
        id="chunk:test",
        source_id="source:test",
        chunk_index=0,
        text=raw_text,
        raw_text=raw_text,
        text_hash="hash",
        token_count=100,
        page_start=1,
        page_end=1,
        heading_path=["Test"],
        content_type="lore",
        profile=profile,
        actuality="AUTHORED",
        canon_scope="SETTING_SOURCE",
        visibility="DM_ONLY",
        indexing_mode="FULL_TEXT",
        extraction_eligible=True,
        remote_processing_allowed=True,
    )


class SchemaTests(unittest.TestCase):
    def test_schema_is_self_consistent(self) -> None:
        registry = SchemaRegistry(SCHEMA)
        self.assertGreaterEqual(len(registry.node_types), 20)
        self.assertGreaterEqual(len(registry.predicates), 40)
        self.assertTrue(registry.pattern_allowed("lore", "MEMBER_OF", "Character", "Faction"))
        self.assertFalse(registry.pattern_allowed("lore", "MEMBER_OF", "Location", "Faction"))

    def test_simple_schema_is_closed(self) -> None:
        output = SchemaRegistry(SCHEMA).neo4j_simple_schema("lore")
        self.assertFalse(output["additional_node_types"])
        self.assertFalse(output["additional_relationship_types"])
        self.assertIn(("Character", "MEMBER_OF", "Faction"), output["patterns"])

    def test_notes_profile_is_closed_and_compact(self) -> None:
        registry = SchemaRegistry(SCHEMA)
        self.assertTrue(registry.pattern_allowed("notes", "PART_OF_PROJECT", "ActionItem", "Project"))
        self.assertFalse(registry.pattern_allowed("notes", "MEMBER_OF", "Person", "Organization"))
        self.assertLessEqual(len(registry.allowed_node_types("notes")), 12)


class ChunkingTests(unittest.TestCase):
    def test_manifest_page_exclusions_remove_overlapping_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.md"
            source_path.write_text(
                "<!-- page: 1 -->\nKeep this lore.\n\n"
                "<!-- page: 2 -->\nRemove this index.\n\n"
                "<!-- page: 3 -->\nKeep this ending.",
                encoding="utf-8",
            )
            spec = source_spec()
            spec.path = str(source_path)
            spec.excludes = [[2, 2]]
            parsed = parse_document(spec, "source:test", ParsingConfig())
            self.assertEqual({1, 3}, {block.page_start for block in parsed.blocks})

    def test_atomic_stat_block_is_not_combined_with_lore(self) -> None:
        blocks = [
            Block("b1", "s", 0, 1, 1, "A long lore paragraph about a city. " * 20, ["City"], "lore"),
            Block(
                "b2",
                "s",
                1,
                2,
                2,
                "Armor Class 15\nHit Points 40\nSpeed 30 ft.\nSTR DEX CON INT WIS CHA\nChallenge Rating 2",
                ["City", "Guard"],
                "statblock",
                True,
            ),
        ]
        chunks = build_chunks(blocks, source_spec(), "source:test", ChunkingConfig(min_tokens=1))
        self.assertEqual(2, len(chunks))
        self.assertEqual("statblock", chunks[1].content_type)
        self.assertTrue(chunks[1].metadata["atomic"])
        self.assertEqual(chunks[0].next_chunk_id, chunks[1].id)
        self.assertEqual(chunks[1].previous_chunk_id, chunks[0].id)

    def test_campaign_source_selects_campaign_profile(self) -> None:
        blocks = [Block("b", "s", 0, 1, 1, "Campaign events happened here. " * 10, [], "lore")]
        chunks = build_chunks(blocks, source_spec("campaign"), "source:test", ChunkingConfig(min_tokens=1))
        self.assertEqual("campaign", chunks[0].profile)
        self.assertEqual("CAMPAIGN", chunks[0].canon_scope)

    def test_note_source_selects_notes_profile(self) -> None:
        spec = source_spec("note")
        spec.canon_scope = "PERSONAL"
        spec.visibility = "PRIVATE"
        blocks = [Block("b", "s", 0, 1, 1, "A decision and follow-up from a meeting. " * 10, [], "lore")]
        chunks = build_chunks(blocks, spec, "source:test", ChunkingConfig(min_tokens=1))
        self.assertEqual("notes", chunks[0].profile)
        self.assertEqual("PERSONAL", chunks[0].canon_scope)

    def test_statblock_parser_emits_domain_valid_relation(self) -> None:
        item = chunk(
            "Iron Defender\nMedium construct\nArmor Class 17\nHit Points 30\n"
            "Speed 40 ft.\nChallenge Rating 1"
        )
        item.content_type = "statblock"
        item.heading_path = ["Iron Defender"]
        result = extract_structured_mechanics(item, "test")
        self.assertEqual({"CreatureArchetype", "StatBlock"}, {e.entity_type for e in result.entities})
        self.assertEqual(["HAS_STAT_BLOCK"], [a.predicate_code for a in result.assertions])
        types = {entity.id: entity.entity_type for entity in result.entities}
        assertion = result.assertions[0]
        self.assertEqual("CreatureArchetype", types[assertion.subject_id])
        self.assertEqual("StatBlock", types[assertion.object_id])

    def test_statblock_parser_pins_declared_ruleset_version(self) -> None:
        item = chunk(
            "Iron Defender\nMedium construct\nArmor Class 17\nHit Points 30\n"
            "Speed 40 ft.\nChallenge Rating 1",
            profile="rules",
        )
        item.content_type = "statblock"
        item.heading_path = ["Iron Defender"]
        item.ruleset_version = "D&D 2024 / SRD 5.2.1"
        result = extract_structured_mechanics(item, "test")
        self.assertIn("RulesetVersion", {entity.entity_type for entity in result.entities})
        self.assertEqual(
            {"HAS_STAT_BLOCK", "FOR_RULESET_VERSION"},
            {assertion.predicate_code for assertion in result.assertions},
        )


class BackboneAndResolutionTests(unittest.TestCase):
    def test_backbone_longest_alias_scan(self) -> None:
        backbone = Backbone(BACKBONE)
        entities, mentions = backbone.scan(chunk("House Lyrandar operates here. Lyrandar sent a courier."))
        ids = {entity.id for entity in entities}
        self.assertIn("urn:dndkg:global:faction:house-lyrandar", ids)
        self.assertEqual(2, len(mentions))
        self.assertEqual("House Lyrandar", mentions[0].surface)

    def test_exact_alias_resolution_is_type_blocked(self) -> None:
        existing = EntityRecord(
            id="faction:vol",
            entity_type="Faction",
            subtype="group",
            canonical_name="Vol",
            aliases=[],
            scope="GLOBAL",
        )
        resolver = EntityResolver([existing], ResolutionConfig(), "test")
        person = ExtractedNode(temp_id="n1", entity_type="Character", name="Vol")
        result = resolver.resolve(person, chunk("Vol appeared."))
        self.assertNotEqual(existing.id, result.entity.id)
        self.assertEqual("Character", result.entity.entity_type)

    def test_campaign_entities_do_not_merge_across_campaigns(self) -> None:
        existing = EntityRecord(
            id="character:alex:a",
            entity_type="Character",
            subtype="player-character",
            canonical_name="Alex",
            aliases=[],
            scope="CAMPAIGN",
            campaign_id="campaign:a",
        )
        item = chunk("Alex arrived.", profile="campaign")
        item.canon_scope = "CAMPAIGN"
        item.campaign_id = "campaign:b"
        result = EntityResolver([existing], ResolutionConfig(), "test").resolve(
            ExtractedNode(
                temp_id="alex",
                entity_type="Character",
                subtype="player-character",
                name="Alex",
            ),
            item,
        )
        self.assertNotEqual(existing.id, result.entity.id)
        self.assertEqual("campaign:b", result.entity.campaign_id)

    def test_record_like_item_instances_require_cross_chunk_review(self) -> None:
        first_chunk = chunk("A potion was found.", profile="character")
        first_chunk.canon_scope = "CAMPAIGN"
        first_chunk.campaign_id = "campaign:a"
        candidate = ExtractedNode(
            temp_id="item",
            entity_type="ItemInstance",
            subtype="other",
            name="Potion",
        )
        first = EntityResolver([], ResolutionConfig(), "test").resolve(candidate, first_chunk)
        second_chunk = chunk("A potion was found again.", profile="character")
        second_chunk.id = "chunk:second"
        second_chunk.canon_scope = "CAMPAIGN"
        second_chunk.campaign_id = "campaign:a"
        second = EntityResolver([first.entity], ResolutionConfig(), "test").resolve(
            candidate, second_chunk
        )
        self.assertNotEqual(first.entity.id, second.entity.id)
        self.assertEqual("INSTANCE_REVIEW", second.method)
        self.assertIsNotNone(second.review_item)


class ValidationAndBatchTests(unittest.TestCase):
    def test_grounding_normalizes_pdf_line_breaks(self) -> None:
        self.assertTrue(quote_is_grounded("light-\nning rail", "The light-\nning rail arrived."))
        self.assertFalse(quote_is_grounded("invented sentence", "Actual sentence."))

    def test_valid_payload_and_unquoted_hallucination(self) -> None:
        registry = SchemaRegistry(SCHEMA)
        text = "Veyra is a member of House Lyrandar."
        payload = ExtractionPayload(
            nodes=[
                ExtractedNode(temp_id="p", entity_type="Character", name="Veyra"),
                ExtractedNode(temp_id="h", entity_type="Faction", name="House Lyrandar"),
            ],
            relationships=[
                ExtractedRelationship(
                    predicate="MEMBER_OF",
                    subject="p",
                    object="h",
                    source_quote=text,
                    confidence=0.9,
                )
            ],
        )
        self.assertEqual([], validate_payload(payload, chunk(text), registry))
        payload.relationships[0].source_quote = "Veyra leads House Lyrandar."
        self.assertTrue(validate_payload(payload, chunk(text), registry))

    def test_batch_file_targets_responses_with_strict_schema(self) -> None:
        registry = SchemaRegistry(SCHEMA)
        backbone = Backbone(BACKBONE)
        from loregraph.config import ExtractionConfig

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "batch.jsonl"
            count = prepare_openai_batch_requests(
                [chunk("Veyra is a member of House Lyrandar.")],
                registry,
                backbone,
                ExtractionConfig(),
                output,
            )
            row = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(1, count)
            self.assertLessEqual(len(row["custom_id"]), 64)
            self.assertEqual("/v1/responses", row["url"])
            self.assertTrue(row["body"]["text"]["format"]["strict"])
            schema = row["body"]["text"]["format"]["schema"]
            self._assert_closed_required_objects(schema)

    def _assert_closed_required_objects(self, value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object" and isinstance(value.get("properties"), dict):
                self.assertFalse(value.get("additionalProperties", True))
                self.assertEqual(set(value["properties"]), set(value.get("required", [])))
            self.assertNotIn("default", value)
            for child in value.values():
                self._assert_closed_required_objects(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_closed_required_objects(child)


if __name__ == "__main__":
    unittest.main()

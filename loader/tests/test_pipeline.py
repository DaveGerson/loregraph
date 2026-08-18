from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from loregraph.audit import audit_store
from loregraph.config import LoaderConfig
from loregraph.obsidian_sink import export_obsidian
from loregraph.pipeline import ingest_manifest
from loregraph.store import RecordStore


ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def test_ingest_is_idempotent_and_obsidian_export_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "notes.md"
            source.write_text(
                "# Session\n\n" + "The party met House Lyrandar in Sharn. " * 25,
                encoding="utf-8",
            )
            manifest = root / "books.yaml"
            manifest.write_text(
                yaml.safe_dump(
                    {
                        "books": [
                            {
                                "path": str(source),
                                "code": "NOTES",
                                "title": "Notes",
                                "edition": "campaign",
                                "source_kind": "campaign",
                                "canon_scope": "CAMPAIGN",
                                "actuality": "OCCURRED",
                                "rights_basis": "USER_OWNED",
                                "indexing_mode": "FULL_TEXT",
                                "visibility": "DM_ONLY",
                                "campaign_id": "campaign:test",
                            }
                        ]
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            config_file = root / "project.toml"
            config_file.write_text(
                f"""
[project]
tenant_id = "test"
workspace_dir = "{(root / 'workspace').as_posix()}"
schema_path = "{(ROOT / 'schema/canonical-profile.json').as_posix()}"
backbone_path = "{(ROOT / 'config/backbone.eberron.json').as_posix()}"

[chunking]
target_tokens = 200
max_tokens = 300
min_tokens = 10
atomic_max_tokens = 1000
overlap_tokens = 0

[obsidian]
enabled = true
vault_path = "{(root / 'vault').as_posix()}"
write_chunk_notes = false
write_assertion_notes = true
max_quote_chars = 500
""".strip(),
                encoding="utf-8",
            )
            config = LoaderConfig.from_toml(config_file)
            first = ingest_manifest(config, manifest)
            second = ingest_manifest(config, manifest)
            self.assertEqual(1, first.sources_parsed)
            self.assertEqual(1, second.sources_skipped_current)
            store = RecordStore(config.workspace_dir)
            self.assertEqual(1, len(store.read("sources")))
            self.assertGreaterEqual(len(store.read("chunks")), 1)
            self.assertGreaterEqual(len(store.read("mentions")), 2)
            old_chunk_ids = {row["id"] for row in store.read("chunks")}
            config.chunking.target_tokens = 80
            config.chunking.max_tokens = 100
            rebuilt = ingest_manifest(config, manifest)
            self.assertEqual(1, rebuilt.sources_parsed)
            new_chunk_ids = {row["id"] for row in store.read("chunks")}
            self.assertTrue(new_chunk_ids)
            self.assertFalse(old_chunk_ids & new_chunk_ids)
            self.assertGreater(len(new_chunk_ids), 1)
            report = audit_store(config)
            self.assertTrue(report.ok, report.errors)

            counts = export_obsidian(config)
            self.assertGreaterEqual(counts["entities"], 2)
            home = root / "vault/00 Dashboard/Knowledge Graph Home.md"
            self.assertTrue(home.exists())
            self.assertIn("LoreGraph Knowledge Base", home.read_text(encoding="utf-8"))
            entity_notes = list((root / "vault/02 Entities").rglob("*.md"))
            self.assertTrue(entity_notes)
            frontmatter = entity_notes[0].read_text(encoding="utf-8").split("---", 2)[1]
            self.assertIsInstance(yaml.safe_load(frontmatter), dict)

    def test_generic_manifest_routes_notes_and_builds_rules_hierarchy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "meeting.md"
            notes.write_text(
                "# Project Northstar\n\nWe decided to ship the pilot. Morgan owns the follow-up.",
                encoding="utf-8",
            )
            rules = root / "rules.md"
            rules.write_text(
                "# Core rules\n\nA test rule belongs to the declared immutable release.",
                encoding="utf-8",
            )
            manifest = root / "sources.yaml"
            manifest.write_text(
                yaml.safe_dump(
                    {
                        "sources": [
                            {
                                "path": str(notes),
                                "code": "MEETING",
                                "title": "Meeting notes",
                                "edition": "v1",
                                "source_kind": "note",
                                "canon_scope": "PERSONAL",
                                "actuality": "AUTHORED",
                                "rights_basis": "USER_OWNED",
                                "indexing_mode": "FULL_TEXT",
                                "visibility": "PRIVATE",
                            },
                            {
                                "path": str(rules),
                                "code": "RULES",
                                "title": "Rules reference",
                                "edition": "v1",
                                "source_kind": "rules",
                                "canon_scope": "NONE",
                                "actuality": "AUTHORED",
                                "rights_basis": "USER_OWNED",
                                "indexing_mode": "FULL_TEXT",
                                "visibility": "PRIVATE",
                                "game_system": "Example System",
                                "ruleset": "Example Core",
                                "ruleset_version": "Example Core 1.0",
                            },
                        ]
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            config_file = root / "project.toml"
            config_file.write_text(
                f"""
[project]
tenant_id = "test"
workspace_dir = "{(root / 'workspace').as_posix()}"
schema_path = "{(ROOT / 'schema/canonical-profile.json').as_posix()}"
backbone_path = "{(ROOT / 'config/backbone.empty.json').as_posix()}"

[chunking]
target_tokens = 200
max_tokens = 300
min_tokens = 1
atomic_max_tokens = 1000
overlap_tokens = 0

[obsidian]
enabled = true
vault_path = "{(root / 'vault').as_posix()}"
write_chunk_notes = false
write_assertion_notes = true
max_quote_chars = 500
""".strip(),
                encoding="utf-8",
            )
            config = LoaderConfig.from_toml(config_file)
            ingest_manifest(config, manifest)
            store = RecordStore(config.workspace_dir)
            self.assertEqual({"notes", "rules"}, {row["profile"] for row in store.read("chunks")})
            self.assertTrue(
                {"GameSystem", "Ruleset", "RulesetVersion"}
                <= {row["entity_type"] for row in store.read("entities")}
            )
            self.assertTrue(
                {"HAS_RULESET", "HAS_RULESET_VERSION"}
                <= {row["predicate_code"] for row in store.read("assertions")}
            )
            report = audit_store(config)
            self.assertTrue(report.ok, report.errors)


if __name__ == "__main__":
    unittest.main()

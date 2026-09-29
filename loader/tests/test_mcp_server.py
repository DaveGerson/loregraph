"""Offline tests for the MCP server's configuration, policy, and Cypher guards.

These need neither Neo4j nor the MCP SDK; live behavior is covered by
test_mcp_integration.py.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loregraph.mcp_server.cypher import (
    check_raw_cypher,
    citation,
    lucene_query,
    name_similarity,
    page_label,
    reciprocal_rank_fusion,
    truncate,
)
from loregraph.mcp_server.embedder import build_embedder
from loregraph.mcp_server.settings import (
    AccessPolicy,
    ConnectionProfile,
    EmbeddingSettings,
    SettingsError,
    load_env_file,
    load_settings,
)

CLEAN_ENV = {
    key: value
    for key, value in os.environ.items()
    if not key.startswith(("NEO4J_", "LOREGRAPH_", "TEST_NEO4J"))
}


def write_toml(root: Path, text: str) -> Path:
    path = root / "project.toml"
    path.write_text(text.strip(), encoding="utf-8")
    return path


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, CLEAN_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_environment_only_connection_defaults_to_player(self) -> None:
        os.environ.update(NEO4J_URI="neo4j://localhost:7687", NEO4J_PASSWORD="secret")
        settings = load_settings(None)
        self.assertEqual("default", settings.default_connection)
        self.assertEqual("player", settings.policy.audience)
        self.assertEqual(("PUBLIC", "PLAYER"), settings.policy.visibilities)
        self.assertEqual("local", settings.connection(None).kind)
        self.assertFalse(settings.allow_raw_cypher)

    def test_missing_connection_is_a_clear_error(self) -> None:
        with self.assertRaisesRegex(SettingsError, "NEO4J_URI"):
            load_settings(None)

    def test_named_connections_local_aura_and_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ca = Path(tmp) / "ca.pem"
            ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
            config = write_toml(
                Path(tmp),
                """
[neo4j]
database = "lore"

[mcp]
default_connection = "aura"

[mcp.policy]
audience = "dm"
campaign_ids = ["campaign:alpha"]

[mcp.connections.local]
uri = "bolt://localhost:7687"
password_env = "LOCAL_PW"

[mcp.connections.aura]
uri_env = "AURA_URI"
password_env = "AURA_PW"
database_env = "AURA_DB"

[mcp.connections.homelab]
uri = "neo4j://graph.example.net:7687"
trusted_ca = "ca.pem"
auth = "bearer"
token_env = "HOMELAB_TOKEN"
""",
            )
            os.environ.update(AURA_URI="neo4j+s://abcd1234.databases.neo4j.io", AURA_PW="pw")
            settings = load_settings(config)
        self.assertEqual({"local", "aura", "homelab"}, set(settings.connections))
        self.assertEqual("aura", settings.connection(None).kind)
        self.assertEqual("remote", settings.connection("homelab").kind)
        self.assertEqual(str(ca.resolve()), settings.connection("homelab").trusted_ca)
        self.assertTrue(settings.connection("homelab").encrypted)
        self.assertEqual("lore", settings.connection("local").resolved_database("lore"))
        self.assertEqual(("campaign:alpha",), settings.policy.campaign_ids)
        self.assertIn("LOCAL_PW", " ".join(settings.connection("local").warnings()))

    def test_cli_arguments_override_file_and_environment(self) -> None:
        os.environ.update(
            NEO4J_URI="neo4j://localhost:7687",
            NEO4J_PASSWORD="pw",
            LOREGRAPH_MCP_AUDIENCE="dm",
        )
        self.assertEqual("dm", load_settings(None).policy.audience)
        self.assertEqual("owner", load_settings(None, audience="owner").policy.audience)

    def test_secrets_never_live_in_project_files_or_uris(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_toml(
                Path(tmp),
                """
[mcp.connections.local]
uri = "neo4j://localhost:7687"
password = "hunter2"
""",
            )
            with self.assertRaisesRegex(SettingsError, "password_env"):
                load_settings(config)
        profile = ConnectionProfile(
            name="x", uri="neo4j://neo4j:hunter2@localhost:7687", password_env="PW"
        )
        with self.assertRaisesRegex(SettingsError, "environment variables"):
            profile.validate()

    def test_describe_redacts_secrets(self) -> None:
        os.environ["PW"] = "hunter2"
        profile = ConnectionProfile(
            name="x", uri="neo4j+s://db.example.com:7687", password_env="PW"
        )
        description = profile.describe()
        self.assertNotIn("hunter2", repr(description))
        self.assertEqual("neo4j+s://db.example.com:7687", description["uri"])
        self.assertEqual([], description["warnings"])

    def test_uri_rules_for_aura_and_custom_trust(self) -> None:
        aura = ConnectionProfile(name="a", uri="neo4j://x.databases.neo4j.io", password_env="PW")
        with self.assertRaisesRegex(SettingsError, "neo4j\\+s"):
            aura.validate()
        bad_scheme = ConnectionProfile(name="h", uri="http://localhost:7474", password_env="PW")
        with self.assertRaisesRegex(SettingsError, "scheme"):
            bad_scheme.validate()
        with tempfile.NamedTemporaryFile(suffix=".pem") as handle:
            mixed = ConnectionProfile(
                name="m", uri="neo4j+s://db.example.com", password_env="PW", trusted_ca=handle.name
            )
            with self.assertRaisesRegex(SettingsError, "trusted_ca"):
                mixed.validate()

    def test_unencrypted_remote_connection_warns(self) -> None:
        os.environ["PW"] = "x"
        profile = ConnectionProfile(name="r", uri="bolt://10.0.0.5:7687", password_env="PW")
        self.assertEqual("remote", profile.kind)
        self.assertTrue(any("Unencrypted" in item for item in profile.warnings()))

    def test_raw_cypher_requires_unrestricted_owner_policy(self) -> None:
        os.environ.update(NEO4J_URI="neo4j://localhost:7687", NEO4J_PASSWORD="pw")
        with self.assertRaisesRegex(SettingsError, "owner"):
            load_settings(None, audience="dm", allow_raw_cypher=True)
        self.assertTrue(
            load_settings(None, audience="owner", allow_raw_cypher=True).allow_raw_cypher
        )

    def test_env_file_loads_without_overriding(self) -> None:
        os.environ["KEEP"] = "original"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\nexport NEO4J_URI='neo4j+s://x.databases.neo4j.io'\n"
                'NEO4J_PASSWORD="p=w"\nKEEP=replaced\n\n',
                encoding="utf-8",
            )
            loaded = load_env_file(path)
        self.assertEqual({"NEO4J_URI", "NEO4J_PASSWORD"}, set(loaded))
        self.assertEqual("neo4j+s://x.databases.neo4j.io", os.environ["NEO4J_URI"])
        self.assertEqual("p=w", os.environ["NEO4J_PASSWORD"])
        self.assertEqual("original", os.environ["KEEP"])

    def test_hosted_query_embeddings_are_never_inferred(self) -> None:
        hosted = EmbeddingSettings(mode="auto", provider="openai", model="text-embedding-3-small")
        self.assertIsNone(build_embedder(hosted))
        local = EmbeddingSettings(mode="auto", provider="sentence_transformers", model="m")
        self.assertEqual("sentence_transformers", build_embedder(local).provider)
        explicit = EmbeddingSettings(
            mode="openai", provider="openai", model="text-embedding-3-small"
        )
        self.assertEqual("openai", build_embedder(explicit).provider)


class PolicyTests(unittest.TestCase):
    def test_audiences_are_nested_and_deny_first(self) -> None:
        player = AccessPolicy.for_audience("player")
        dm = AccessPolicy.for_audience("dm")
        owner = AccessPolicy.for_audience("owner")
        self.assertLess(set(player.visibilities), set(dm.visibilities))
        self.assertLess(set(dm.visibilities), set(owner.visibilities))
        self.assertNotIn("REJECTED", owner.review_statuses)
        self.assertFalse(dm.unrestricted)
        self.assertTrue(owner.unrestricted)

    def test_configured_visibilities_cannot_exceed_audience(self) -> None:
        with self.assertRaisesRegex(SettingsError, "exceed"):
            AccessPolicy.for_audience("player", visibilities=["PLAYER", "DM_ONLY"])
        notes_only = AccessPolicy.for_audience("owner", visibilities=["PRIVATE"])
        self.assertEqual(("PRIVATE",), notes_only.visibilities)
        self.assertFalse(notes_only.unrestricted)

    def test_narrow_only_restricts(self) -> None:
        policy = AccessPolicy.for_audience("dm", campaign_ids=["campaign:a", "campaign:b"])
        self.assertEqual(("PLAYER",), policy.narrow(visibilities=["PLAYER"]).visibilities)
        self.assertEqual(("campaign:a",), policy.narrow(campaign_id="campaign:a").campaign_ids)
        with self.assertRaisesRegex(SettingsError, "not permitted"):
            policy.narrow(visibilities=["PRIVATE"])
        with self.assertRaisesRegex(SettingsError, "not permitted"):
            policy.narrow(campaign_id="campaign:c")
        with self.assertRaisesRegex(SettingsError, "excluded"):
            policy.narrow(review_statuses=["REJECTED"])
        scoped = AccessPolicy.for_audience("dm", canon_scopes=["CAMPAIGN"])
        with self.assertRaisesRegex(SettingsError, "excluded"):
            scoped.narrow(canon_scopes=["PERSONAL"])

    def test_parameters_match_cypher_names(self) -> None:
        params = AccessPolicy.for_audience("player").parameters()
        self.assertEqual(
            {"visibilities", "reviewStatuses", "canonScopes", "campaignIds"}, set(params)
        )


class CypherHelperTests(unittest.TestCase):
    def test_lucene_query_escapes_syntax(self) -> None:
        query = lucene_query('House "Lyrandar" AND (airships)~ OR title:x* half-elf')
        self.assertEqual(
            '"House Lyrandar airships title x half\\-elf"^4 OR House OR Lyrandar OR airships '
            "OR title OR x OR half\\-elf",
            query,
        )
        with self.assertRaises(ValueError):
            lucene_query("  ?!  ")

    def test_lucene_query_fuzzy_only_for_longer_terms(self) -> None:
        query = lucene_query("Veyra of Sharn", fuzzy=True)
        self.assertIn("Veyra~1", query)
        self.assertNotIn("of~1", query)

    def test_name_similarity_rejects_different_entities(self) -> None:
        self.assertEqual(1.0, name_similarity("house lyrandar", ["House Lyrandar"]))
        self.assertGreaterEqual(name_similarity("Veyra", ["Captain Veyra"]), 0.85)
        self.assertGreaterEqual(name_similarity("Lyrandr", ["House Lyrandar", "Lyrandar"]), 0.85)
        self.assertLess(name_similarity("House Cannith", ["House Lyrandar", "Lyrandar"]), 0.85)

    def test_reciprocal_rank_fusion_rewards_agreement(self) -> None:
        fused = reciprocal_rank_fusion({"fulltext": ["a", "b", "c"], "vector": ["b", "d"]})
        self.assertEqual("b", fused[0][0])
        self.assertEqual(["fulltext", "vector"], fused[0][2])
        self.assertEqual({"a", "b", "c", "d"}, {item for item, _, _ in fused})

    def test_citation_carries_pages_heading_and_attribution(self) -> None:
        source = {
            "title": "SRD",
            "edition": "5.2.1",
            "indexing_mode": "FULL_TEXT_WITH_ATTRIBUTION",
            "attribution": "CC-BY-4.0",
            "current": True,
        }
        self.assertEqual(
            "SRD (5.2.1), pp. 3-4 — Spells > Fireball. CC-BY-4.0",
            citation(source, page_label(3, 4), "Spells > Fireball"),
        )
        self.assertIn("[superseded version]", citation({**source, "current": False}, "p. 1", None))
        self.assertEqual("p. 7", page_label(7, 7))

    def test_truncate_marks_cut_text(self) -> None:
        text, cut = truncate("word " * 100, 50)
        self.assertTrue(cut)
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(len(text), 52)
        self.assertEqual(("short", False), truncate("short", 50))

    def test_raw_cypher_guard(self) -> None:
        self.assertEqual("MATCH (n) RETURN n", check_raw_cypher("MATCH (n) RETURN n;"))
        check_raw_cypher(
            "CALL db.index.fulltext.queryNodes('entity_search', $q) YIELD node RETURN node"
        )
        check_raw_cypher("MATCH (n) CALL { WITH n RETURN 1 AS x } RETURN x")
        rejected = [
            "LOAD CSV FROM 'file:///etc/passwd' AS row RETURN row",
            "load   csv with headers from $url as r return r",
            "CALL apoc.load.json('http://169.254.169.254/')",
            "CALL /* c */ `apoc`.`load`.json('http://x')",
            "RETURN apoc.cypher.runFirstColumnSingle('MATCH (n) DETACH DELETE n', {})",
            "CALL dbms.listConfig()",
            "CALL gds.graph.project('g', '*', '*')",
            "CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'DELETE n', {})",
            "CALL db.create.setNodeVectorProperty(n, 'v', [1])",
            "MATCH (n) RETURN n; MATCH (m) DETACH DELETE m",
            "EXPLAIN MATCH (n) RETURN n",
            "USE system SHOW USERS",
            "",
        ]
        for query in rejected:
            with self.subTest(query=query), self.assertRaises(ValueError):
                check_raw_cypher(query)


if __name__ == "__main__":
    unittest.main()

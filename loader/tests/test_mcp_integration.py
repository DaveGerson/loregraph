"""Live tests for the MCP read server against a disposable Neo4j database.

Opt in with a throwaway database, because the fixture load replaces every
LoreGraph-managed node in it:

    docker run -d -p 7687:7687 -e NEO4J_AUTH=neo4j/loregraph-test neo4j:5-community
    LOREGRAPH_TEST_NEO4J_URI=neo4j://localhost:7687 \\
    LOREGRAPH_TEST_NEO4J_PASSWORD=loregraph-test \\
    PYTHONPATH=src python -m unittest tests.test_mcp_integration -v
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

TEST_URI = os.getenv("LOREGRAPH_TEST_NEO4J_URI", "")
TEST_PASSWORD = os.getenv("LOREGRAPH_TEST_NEO4J_PASSWORD", "")

try:
    import mcp  # noqa: F401
    import neo4j  # noqa: F401

    DEPENDENCIES = True
except ImportError:
    DEPENDENCIES = False


@unittest.skipUnless(TEST_URI and TEST_PASSWORD, "set LOREGRAPH_TEST_NEO4J_URI/PASSWORD to run")
@unittest.skipUnless(DEPENDENCIES, "install the mcp extra to run")
class LiveNeo4jTests(unittest.TestCase):
    readers: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from mcp_fixture import HashingEmbedder, build_ledger
        except ImportError:  # run as tests.test_mcp_integration from the loader root
            from tests.mcp_fixture import HashingEmbedder, build_ledger

        from loregraph.mcp_server.graph import GraphGateway
        from loregraph.mcp_server.retrieval import LoreGraphReader
        from loregraph.mcp_server.settings import load_settings
        from loregraph.neo4j_sink import load_neo4j

        cls.env = mock.patch.dict(
            os.environ,
            {
                "NEO4J_URI": TEST_URI,
                "NEO4J_USERNAME": os.getenv("LOREGRAPH_TEST_NEO4J_USERNAME", "neo4j"),
                "NEO4J_PASSWORD": TEST_PASSWORD,
                "NEO4J_DATABASE": os.getenv("LOREGRAPH_TEST_NEO4J_DATABASE", "neo4j"),
            },
        )
        cls.env.start()
        cls.tmp = tempfile.TemporaryDirectory()
        config = build_ledger(Path(cls.tmp.name))
        load_neo4j(config)
        cls.readers = {}
        for audience in ("player", "dm", "owner"):
            settings = load_settings(
                None, audience=audience, allow_raw_cypher=True if audience == "owner" else None
            )
            cls.readers[audience] = LoreGraphReader(
                settings, GraphGateway(settings), HashingEmbedder(), "test"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        for reader in cls.readers.values():
            reader.gateway.close()
        cls.tmp.cleanup()
        cls.env.stop()

    def search_text(self, audience: str, query: str, **kwargs: Any) -> str:
        results = self.readers[audience].search_passages(query, max_chars=4000, **kwargs)["results"]
        return "\n".join(item["text"] for item in results)

    # -- status and capabilities -------------------------------------------------

    def test_status_reports_indexes_and_vector_search(self) -> None:
        status = self.readers["player"].status()
        self.assertTrue(status["neo4j"]["reachable"])
        self.assertTrue(status["loregraph_projection"])
        self.assertEqual("ONLINE", status["retrieval"]["fulltext"]["chunk_fulltext"])
        self.assertTrue(status["retrieval"]["vector_search"])
        self.assertNotIn(TEST_PASSWORD, repr(status))

    # -- leakage -------------------------------------------------------------------

    def test_player_never_sees_dm_or_private_text(self) -> None:
        for mode in ("fulltext", "vector", "hybrid"):
            text = self.search_text(
                "player", "Cannith secretly copied manifest zeppelin heist", mode=mode, limit=25
            )
            self.assertNotIn("secretly", text, mode)
            self.assertNotIn("zeppelin", text, mode)
        self.assertIn("secretly", self.search_text("dm", "secretly Cannith", mode="fulltext"))
        self.assertNotIn("zeppelin", self.search_text("dm", "zeppelin heist", mode="fulltext"))
        self.assertIn("zeppelin", self.search_text("owner", "zeppelin heist", mode="fulltext"))

    def test_player_entity_views_exclude_secret_assertions_and_entities(self) -> None:
        player = self.readers["player"]
        profile = player.get_entity("Captain Veyra")
        statements = {item["statement"] for item in profile["assertions"]}
        self.assertEqual({"Captain Veyra MEMBER_OF House Lyrandar"}, statements)
        self.assertTrue(profile["entity"]["description"].startswith("[withheld"))
        self.assertEqual([], player.search_entities("Zell")["results"])
        self.assertEqual([], player.search_entities("Cannith")["results"])
        with self.assertRaises(LookupError):
            player.find_connections("Captain Veyra", "House Cannith")
        dm_statements = {
            item["statement"]
            for item in self.readers["dm"].find_assertions(entity="Captain Veyra")["results"]
        }
        self.assertIn("Captain Veyra MEMBER_OF House Cannith", dm_statements)
        owner_entity = self.readers["owner"].get_entity("Captain Veyra")["entity"]
        self.assertEqual("Secret dossier on Captain Veyra", owner_entity["description"])

    def test_hidden_passages_look_missing(self) -> None:
        dm_hit = self.readers["dm"].search_passages("secretly Cannith", mode="fulltext")["results"][
            0
        ]
        with self.assertRaisesRegex(LookupError, "not visible"):
            self.readers["player"].get_passage(dm_hit["chunk_id"])
        self.assertEqual(
            "target", self.readers["dm"].get_passage(dm_hit["chunk_id"])["passages"][0]["role"]
        )

    def test_rejected_assertions_are_never_returned(self) -> None:
        results = self.readers["owner"].find_assertions(predicates=["MEMBER_OF"], limit=50)[
            "results"
        ]
        self.assertNotIn("REJECTED", {item["review_status"] for item in results})
        self.assertEqual([], self.readers["owner"].search_entities("Zell")["results"])

    # -- campaign isolation --------------------------------------------------------

    def test_campaign_filter_isolates_campaigns(self) -> None:
        player = self.readers["player"]
        alpha = player.find_assertions(campaign_id="campaign:alpha", canon_scopes=["CAMPAIGN"])[
            "results"
        ]
        self.assertEqual(["Mira LOCATED_IN Sharn"], [item["statement"] for item in alpha])
        self.assertEqual(
            [], player.search_entities("Doran", campaign_id="campaign:alpha")["results"]
        )
        self.assertEqual(
            1, len(player.search_entities("Doran", campaign_id="campaign:beta")["results"])
        )
        text = self.search_text(
            "player", "Doran oath Mira lightning", campaign_id="campaign:alpha", limit=25
        )
        self.assertIn("Mira", text)
        self.assertNotIn("Doran", text)

    # -- retrieval quality ------------------------------------------------------------

    def test_passage_results_carry_citations_and_neighbors(self) -> None:
        result = self.readers["player"].search_passages("Veyra Lyrandar airship docks", limit=3)
        self.assertEqual("hybrid", result["mode"])
        top = result["results"][0]
        self.assertIn("Gazetteer of Sharn (v1), p. 2", top["citation"])
        self.assertIn("CC-BY-4.0", top["citation"])
        self.assertEqual("SETTING", top["source_code"])
        context = self.readers["player"].get_passage(top["chunk_id"], neighbors=2)["passages"]
        self.assertEqual(
            {"target"}, {item["role"] for item in context if item["chunk_id"] == top["chunk_id"]}
        )
        self.assertGreater(len(context), 1)

    def test_embedder_failure_falls_back_to_fulltext(self) -> None:
        from loregraph.mcp_server.retrieval import LoreGraphReader

        class BrokenEmbedder:
            provider = "test"
            model = "test-hashing-64"

            def embed(self, text: str) -> list[float]:
                raise RuntimeError("model weights missing")

        base = self.readers["player"]
        reader = LoreGraphReader(base.settings, base.gateway, BrokenEmbedder(), "test")
        result = reader.search_passages("Veyra airship")
        self.assertEqual("fulltext", result["mode"])
        self.assertTrue(result["results"])
        self.assertIn("model weights missing", result["notes"][0])
        with self.assertRaisesRegex(RuntimeError, "embedding failed"):
            reader.search_passages("Veyra", mode="vector")

    def test_assertions_include_grounded_evidence(self) -> None:
        results = self.readers["player"].find_assertions(entity="Veyra", predicates=["MEMBER_OF"])[
            "results"
        ]
        self.assertEqual(1, len(results))
        evidence = results[0]["evidence"][0]
        self.assertEqual("Captain Veyra is a member of House Lyrandar.", evidence["quote"])
        self.assertIn("p. 2", evidence["citation"])

    def test_find_connections_walks_permitted_assertions(self) -> None:
        paths = self.readers["player"].find_connections("Captain Veyra", "Sharn")["paths"]
        self.assertEqual(
            ["MEMBER_OF", "OPERATES_IN"], [step["predicate"] for step in paths[0]["steps"]]
        )

    def test_name_resolution_does_not_substitute_entities(self) -> None:
        reader = self.readers["player"]
        self.assertEqual("close_match", reader.get_entity("Lyrandr")["resolution"]["method"])
        with self.assertRaisesRegex(LookupError, "No visible entity"):
            reader.get_entity("House Cannith")

    def test_graph_schema_counts_only_visible_records(self) -> None:
        player = self.readers["player"].graph_schema()
        owner = self.readers["owner"].graph_schema()
        self.assertLess(player["visible_counts"]["chunks"], owner["visible_counts"]["chunks"])
        self.assertEqual(
            {"campaign:alpha", "campaign:beta"}, {row["campaign_id"] for row in player["campaigns"]}
        )
        self.assertNotIn("database_labels", player)
        self.assertIn("KnowledgeChunk", owner["database_labels"])

    # -- raw Cypher ------------------------------------------------------------------

    def test_raw_cypher_is_read_only(self) -> None:
        owner = self.readers["owner"]
        rows = owner.read_cypher(
            "MATCH (e:Character {name: $name}) RETURN e.name AS name", parameters={"name": "Mira"}
        )["rows"]
        self.assertEqual([{"name": "Mira"}], rows)
        for query in (
            "CREATE (n:Hack) RETURN n",
            "MATCH (n) DETACH DELETE n",
            "CREATE INDEX x FOR (n:X) ON (n.y)",
        ):
            with self.subTest(query=query), self.assertRaises(PermissionError):
                owner.read_cypher(query)
        with self.assertRaises(PermissionError):
            self.readers["dm"].read_cypher("MATCH (n) RETURN n")
        self.assertEqual([], owner.read_cypher("MATCH (n:Hack) RETURN n")["rows"])

    def test_timeouts_are_enforced(self) -> None:
        from loregraph.mcp_server.graph import GraphError

        owner = self.readers["owner"]
        original = owner.settings.query_timeout_seconds
        owner.settings.query_timeout_seconds = 0.5
        try:
            with self.assertRaisesRegex(GraphError, "timeout"):
                owner.read_cypher(
                    "UNWIND range(1, 300000000) AS x WITH x WHERE x < 0 RETURN count(x)"
                )
        finally:
            owner.settings.query_timeout_seconds = original

    # -- MCP protocol ------------------------------------------------------------------

    def test_mcp_tools_over_in_memory_client(self) -> None:
        from mcp import Client

        from loregraph.mcp_server.server import build_server

        async def run(audience: str) -> tuple[list[str], Any, Any]:
            reader = self.readers[audience]
            async with Client(build_server(reader.settings, reader)) as client:
                tools = [tool.name for tool in (await client.list_tools()).tools]
                ok = await client.call_tool("search_entities", {"query": "Sharn"})
                bad = await client.call_tool("get_entity", {"entity": "urn:loregraph:missing"})
                return tools, ok, bad

        tools, ok, bad = asyncio.run(run("player"))
        self.assertNotIn("read_cypher", tools)
        self.assertIn("search_passages", tools)
        self.assertFalse(ok.is_error)
        self.assertEqual("Sharn", ok.structured_content["results"][0]["name"])
        self.assertTrue(bad.is_error)
        self.assertIn("not visible", bad.content[0].text)
        owner_tools, _, _ = asyncio.run(run("owner"))
        self.assertIn("read_cypher", owner_tools)


if __name__ == "__main__":
    unittest.main()

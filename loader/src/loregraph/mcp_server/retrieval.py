"""LoreGraph-aware retrieval operations behind the MCP tools.

Each public method returns plain JSON-ready data. Access policy is applied
inside every Cypher statement (deny-first), and again when results are
hydrated, so prohibited records never leave the database.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .cypher import (
    DATA_DICTIONARY,
    TEXT_INDEXING_MODES,
    assertion_filter,
    check_raw_cypher,
    chunk_filter,
    citation,
    entity_filter,
    lucene_query,
    name_similarity,
    page_label,
    reciprocal_rank_fusion,
    snippet,
    truncate,
)
from .embedder import QueryEmbedder
from .graph import GraphError
from .settings import AccessPolicy, ServerSettings

CAPABILITY_TTL_SECONDS = 60.0
NAME_MATCH_THRESHOLD = 0.85

_ENTITY_FIELDS = """
  e.id AS id, e.name AS name, [label IN labels(e) WHERE label <> 'Entity'] AS types,
  e.subtype AS subtype, coalesce(e.aliases, []) AS aliases, e.scope AS scope,
  e.campaignId AS campaignId, e.provisional AS provisional, e.confidence AS confidence,
  e.description AS description, e.structuredData AS structuredData
"""

_CHUNK_FIELDS = """
  c{.id, .text, .headingPath, .pageStart, .pageEnd, .contentType, .profile, .visibility,
    .canonScope, .actuality, .campaignId, .status, .chunkIndex, .sourceId} AS chunk
"""


class NotVisibleError(LookupError):
    """Raised for records that are missing or hidden by policy (indistinguishable)."""


class LoreGraphReader:
    def __init__(
        self,
        settings: ServerSettings,
        gateway: Any,
        embedder: QueryEmbedder | None = None,
        server_version: str = "",
    ):
        self.settings = settings
        self.gateway = gateway
        self.embedder = embedder
        self.server_version = server_version
        self._capabilities: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._schema = _load_schema(settings.schema_path)

    # ------------------------------------------------------------------ helpers

    def _policy(
        self,
        campaign_id: str | None = None,
        canon_scopes: list[str] | None = None,
        review_statuses: list[str] | None = None,
        visibilities: list[str] | None = None,
    ) -> AccessPolicy:
        return self.settings.policy.narrow(
            campaign_id=campaign_id,
            canon_scopes=canon_scopes,
            review_statuses=review_statuses,
            visibilities=visibilities,
        )

    @staticmethod
    def _params(
        policy: AccessPolicy, include_superseded: bool = False, **extra: Any
    ) -> dict[str, Any]:
        params = policy.parameters()
        params["textIndexingModes"] = list(TEXT_INDEXING_MODES)
        params["includeSuperseded"] = include_superseded
        params.update(extra)
        return params

    def _read(
        self,
        query: str,
        params: dict[str, Any],
        connection: str | None,
        max_rows: int | None = None,
    ):
        return self.gateway.read(query, params, connection=connection, max_rows=max_rows)

    def capabilities(
        self, connection: str | None = None, *, refresh: bool = False
    ) -> dict[str, Any]:
        """Index and embedding facts, cached briefly per connection."""

        key = self.settings.connection(connection).name
        now = time.monotonic()
        with self._lock:
            cached = self._capabilities.get(key)
            if cached and not refresh and now - cached[0] < CAPABILITY_TTL_SECONDS:
                return cached[1]
        indexes = self._read(
            "SHOW INDEXES YIELD name, type, state, options RETURN name, type, state, options",
            {},
            connection,
        ).rows
        by_name = {row["name"]: row for row in indexes}
        vector_name = self.settings.embeddings.vector_index_name
        vector = by_name.get(vector_name)
        dimensions = None
        if vector:
            config = (vector.get("options") or {}).get("indexConfig") or {}
            dimensions = config.get("vector.dimensions")
        models = self._read(
            "MATCH (c:KnowledgeChunk) WHERE c.embeddingModel IS NOT NULL "
            "RETURN c.embeddingModel AS model, count(*) AS chunks ORDER BY chunks DESC",
            {},
            connection,
        ).rows
        indexed_models = [row["model"] for row in models]
        vector_ready = bool(vector and vector.get("state") == "ONLINE")
        reason = None
        if not vector_ready:
            reason = f"vector index {vector_name!r} is missing or not ONLINE"
        elif self.embedder is None:
            reason = "no query embedder configured (see [embeddings] and mcp.query_embeddings)"
        elif indexed_models and self.embedder.model not in indexed_models:
            reason = (
                f"query model {self.embedder.model!r} differs from indexed model(s) "
                f"{indexed_models}; vectors from different models are not comparable"
            )
        capabilities = {
            "fulltext": {
                name: (by_name[name]["state"] if name in by_name else "MISSING")
                for name in (self.settings.entity_index, self.settings.chunk_index)
            },
            "vector_index": {
                "name": vector_name,
                "state": vector.get("state") if vector else "MISSING",
                "dimensions": dimensions,
            },
            "indexed_embedding_models": models,
            "query_embedder": (
                {"provider": self.embedder.provider, "model": self.embedder.model}
                if self.embedder
                else None
            ),
            "vector_search": reason is None,
            "vector_search_unavailable_reason": reason,
        }
        with self._lock:
            self._capabilities[key] = (now, capabilities)
        return capabilities

    # ------------------------------------------------------------------- status

    def status(self, connection: str | None = None) -> dict[str, Any]:
        profile = self.settings.connection(connection)
        output: dict[str, Any] = {
            "server": {"name": "loregraph", "version": self.server_version},
            "connection": profile.describe(self.settings.fallback_database),
            "configured_connections": {
                name: item.kind for name, item in sorted(self.settings.connections.items())
            },
            "default_connection": self.settings.default_connection,
            "policy": self.settings.policy.to_dict(),
            "limits": {
                "max_rows": self.settings.max_rows,
                "query_timeout_seconds": self.settings.query_timeout_seconds,
            },
            "raw_cypher_enabled": self.settings.allow_raw_cypher,
        }
        try:
            output["neo4j"] = {"reachable": True, **self.gateway.server_info(profile.name)}
        except Exception as exc:  # report, do not fail: status is the diagnostic tool
            output["neo4j"] = {"reachable": False, "error": str(exc)}
            return output
        try:
            counts = self._read(
                "OPTIONAL MATCH (c:KnowledgeChunk) WITH count(c) AS chunks "
                "OPTIONAL MATCH (a:Assertion) RETURN chunks, count(a) AS assertions",
                {},
                profile.name,
            ).rows[0]
            output["loregraph_projection"] = bool(counts["chunks"] or counts["assertions"])
            if not output["loregraph_projection"]:
                output["hint"] = (
                    "No LoreGraph records found in this database. Run `loregraph load-neo4j` "
                    "against it, or check the database name."
                )
            output["retrieval"] = self.capabilities(profile.name, refresh=True)
        except GraphError as exc:
            output["retrieval_error"] = str(exc)
        return output

    # ----------------------------------------------------------------- entities

    def search_entities(
        self,
        query: str,
        *,
        entity_types: list[str] | None = None,
        campaign_id: str | None = None,
        limit: int = 10,
        connection: str | None = None,
    ) -> dict[str, Any]:
        policy = self._policy(campaign_id=campaign_id)
        rows = self._entity_hits(query, policy, entity_types, limit, connection)
        return {
            "query": query,
            "results": [
                self._entity(row, policy)
                | {"score": round(row["score"], 4), "visible_mentions": row["visibleMentions"]}
                for row in rows
            ],
        }

    def _entity_hits(
        self,
        query: str,
        policy: AccessPolicy,
        entity_types: list[str] | None,
        limit: int,
        connection: str | None,
    ) -> list[dict[str, Any]]:
        statement = f"""
        CALL db.index.fulltext.queryNodes($index, $lucene, {{limit: $fetch}})
        YIELD node AS e, score
        WHERE {entity_filter("e")}
          AND ($types IS NULL OR any(label IN labels(e) WHERE label IN $types))
        RETURN {_ENTITY_FIELDS}, score,
          size([(c:KnowledgeChunk)-[:MENTIONS]->(e) WHERE {chunk_filter("c")} | c]) AS visibleMentions
        ORDER BY score DESC, visibleMentions DESC
        LIMIT $limit
        """
        params = self._params(
            policy,
            index=self.settings.entity_index,
            lucene=lucene_query(query, fuzzy=True),
            fetch=min(max(limit * 5, 25), 250),
            types=entity_types or None,
            limit=limit,
        )
        return self._read(statement, params, connection).rows

    def resolve_entity(
        self, ref: str, policy: AccessPolicy, connection: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve an entity id or a name/alias to one visible entity row."""

        ref = ref.strip()
        if not ref:
            raise ValueError("An entity id or name is required")
        if ref.startswith("urn:") or (":" in ref and " " not in ref):
            rows = self._read(
                f"MATCH (e:Entity {{id: $id}}) WHERE {entity_filter('e')} RETURN {_ENTITY_FIELDS}",
                self._params(policy, id=ref),
                connection,
            ).rows
            if rows:
                return rows[0], {"input": ref, "method": "id"}
            if ref.startswith("urn:"):
                raise NotVisibleError(
                    f"Entity {ref!r} was not found or is not visible under the current access policy"
                )
        hits = self._entity_hits(ref, policy, None, 6, connection)
        scored = [(name_similarity(ref, [row["name"], *row["aliases"]]), row) for row in hits]
        exact = [row for score, row in scored if score == 1.0]
        if len(exact) > 1:
            candidates = ", ".join(f"{row['name']} [{_type(row)}] {row['id']}" for row in exact)
            raise LookupError(f"{ref!r} is ambiguous; pass one of these ids: {candidates}")
        close = [row for score, row in scored if score >= NAME_MATCH_THRESHOLD]
        if exact:
            chosen, method = exact[0], "exact_name"
        elif close:
            chosen, method = close[0], "close_match"
        else:
            # Never substitute a different entity for a name we could not find:
            # the wanted one may simply be hidden by policy.
            nearby = "; ".join(f"{row['name']} [{_type(row)}] {row['id']}" for row in hits[:3])
            suffix = f" Nearest visible names: {nearby}." if nearby else ""
            raise NotVisibleError(f"No visible entity matches {ref!r}.{suffix}")
        alternatives = [
            {"id": row["id"], "name": row["name"], "type": _type(row)}
            for row in hits
            if row["id"] != chosen["id"]
        ][:4]
        return chosen, {"input": ref, "method": method, "alternatives": alternatives}

    def get_entity(
        self,
        entity: str,
        *,
        campaign_id: str | None = None,
        assertion_limit: int = 20,
        passage_limit: int = 5,
        connection: str | None = None,
    ) -> dict[str, Any]:
        policy = self._policy(campaign_id=campaign_id)
        row, resolution = self.resolve_entity(entity, policy, connection)
        entity_id = row["id"]
        summary_rows = self._read(
            f"""
            MATCH (e:Entity {{id: $id}})<-[role:SUBJECT|OBJECT]-(a:Assertion)
            WHERE {assertion_filter("a")}
            RETURN a.predicateCode AS predicate,
                   CASE type(role) WHEN 'SUBJECT' THEN 'outgoing' ELSE 'incoming' END AS direction,
                   count(DISTINCT a) AS count
            ORDER BY count DESC, predicate
            """,
            self._params(policy, id=entity_id),
            connection,
        ).rows
        passages = self._read(
            f"""
            MATCH (c:KnowledgeChunk)-[m:MENTIONS]->(e:Entity {{id: $id}})
            WHERE {chunk_filter("c")}
            WITH c, collect(m.surface)[0] AS surface, count(m) AS mentions
            RETURN {_CHUNK_FIELDS}, surface, mentions
            ORDER BY mentions DESC, c.sourceId, c.chunkIndex
            LIMIT $limit
            """,
            self._params(policy, id=entity_id, limit=passage_limit),
            connection,
        ).rows
        total_mentions = self._read(
            f"""
            MATCH (c:KnowledgeChunk)-[:MENTIONS]->(e:Entity {{id: $id}})
            WHERE {chunk_filter("c")}
            RETURN count(DISTINCT c) AS chunks
            """,
            self._params(policy, id=entity_id),
            connection,
        ).rows[0]["chunks"]
        sources = self._sources({item["chunk"]["sourceId"] for item in passages}, connection)
        assertions = self._assertions(
            policy,
            connection,
            entity_id=entity_id,
            limit=assertion_limit,
        )
        return {
            "entity": self._entity(row, policy, full=True),
            "resolution": resolution,
            "assertion_summary": summary_rows,
            "assertions": assertions["results"],
            "assertions_truncated": assertions["truncated"],
            "mentioned_in_passages": total_mentions,
            "passages": [
                {
                    **self._passage_header(item["chunk"], sources),
                    "mentions_here": item["mentions"],
                    "snippet": snippet(item["chunk"]["text"] or "", item["surface"]),
                }
                for item in passages
            ],
        }

    # --------------------------------------------------------------- assertions

    def find_assertions(
        self,
        *,
        entity: str | None = None,
        predicates: list[str] | None = None,
        direction: str = "any",
        other_entity: str | None = None,
        campaign_id: str | None = None,
        canon_scopes: list[str] | None = None,
        actualities: list[str] | None = None,
        truth_statuses: list[str] | None = None,
        review_statuses: list[str] | None = None,
        include_superseded: bool = False,
        limit: int = 25,
        connection: str | None = None,
    ) -> dict[str, Any]:
        policy = self._policy(
            campaign_id=campaign_id, canon_scopes=canon_scopes, review_statuses=review_statuses
        )
        resolutions = {}
        entity_id = other_id = None
        if entity:
            row, resolutions["entity"] = self.resolve_entity(entity, policy, connection)
            entity_id = row["id"]
        if other_entity:
            row, resolutions["other_entity"] = self.resolve_entity(other_entity, policy, connection)
            other_id = row["id"]
        result = self._assertions(
            policy,
            connection,
            entity_id=entity_id,
            other_id=other_id,
            predicates=predicates,
            direction=direction,
            actualities=actualities,
            truth_statuses=truth_statuses,
            include_superseded=include_superseded,
            limit=limit,
        )
        return {"resolution": resolutions, **result}

    def _assertions(
        self,
        policy: AccessPolicy,
        connection: str | None,
        *,
        entity_id: str | None = None,
        other_id: str | None = None,
        assertion_ids: list[str] | None = None,
        predicates: list[str] | None = None,
        direction: str = "any",
        actualities: list[str] | None = None,
        truth_statuses: list[str] | None = None,
        include_superseded: bool = False,
        limit: int = 25,
        evidence_limit: int = 3,
    ) -> dict[str, Any]:
        if direction not in {"any", "outgoing", "incoming"}:
            raise ValueError("direction must be any, outgoing, or incoming")
        if assertion_ids is not None:
            anchor = (
                "UNWIND $assertionIds AS assertionId MATCH (a:Assertion {id: assertionId}) WITH a"
            )
        elif entity_id:
            anchor = """
            MATCH (:Entity {id: $entityId})<-[role:SUBJECT|OBJECT]-(a:Assertion)
            WHERE $direction = 'any'
               OR (type(role) = 'SUBJECT' AND $direction = 'outgoing')
               OR (type(role) = 'OBJECT' AND $direction = 'incoming')
            WITH DISTINCT a
            """
        else:
            anchor = "MATCH (a:Assertion) WITH a"
        statement = f"""
        {anchor}
        WHERE {assertion_filter("a")}
          AND ($predicates IS NULL OR a.predicateCode IN $predicates)
          AND ($actualities IS NULL OR a.actuality IN $actualities)
          AND ($truthStatuses IS NULL OR a.truthStatus IN $truthStatuses)
          AND ($otherId IS NULL OR EXISTS {{ MATCH (a)-[:SUBJECT|OBJECT]->(:Entity {{id: $otherId}}) }})
        WITH a
        ORDER BY CASE a.reviewStatus WHEN 'ACCEPTED' THEN 0 ELSE 1 END,
                 a.confidence DESC, a.predicateCode, a.id
        LIMIT $fetch
        MATCH (a)-[:SUBJECT]->(s:Entity)
        OPTIONAL MATCH (a)-[:OBJECT]->(o:Entity)
        OPTIONAL MATCH (a)-[:LITERAL_OBJECT]->(literal:LiteralValue)
        OPTIONAL MATCH (a)-[:ASSERTED_BY]->(author:Entity)
        RETURN a{{.id, .predicateCode, .truthStatus, .reviewStatus, .actuality, .canonScope,
                  .visibility, .campaignId, .confidence, .derivationMethod, .validFrom,
                  .validTo, .active}} AS assertion,
               s{{.id, .name, types: [label IN labels(s) WHERE label <> 'Entity']}} AS subject,
               CASE WHEN o IS NULL THEN NULL
                    ELSE o{{.id, .name, types: [label IN labels(o) WHERE label <> 'Entity']}} END AS object,
               literal.value AS literal,
               CASE WHEN author IS NULL THEN NULL ELSE author{{.id, .name}} END AS assertedBy,
               [(a)-[:SUPPORTED_BY]->(f:SourceFragment)-[:EVIDENCE_IN_CHUNK]->(c:KnowledgeChunk)
                  WHERE {chunk_filter("c")}
                  | {{quote: f.selectorValue, fragmentId: f.id, chunkId: c.id,
                     pageStart: coalesce(f.pageStart, c.pageStart),
                     pageEnd: coalesce(f.pageEnd, c.pageEnd),
                     heading: c.headingPath, sourceId: c.sourceId}}][..$evidenceLimit] AS evidence
        """
        fetch = limit + 1
        params = self._params(
            policy,
            include_superseded,
            entityId=entity_id,
            otherId=other_id,
            assertionIds=assertion_ids,
            predicates=predicates or None,
            direction=direction,
            actualities=actualities or None,
            truthStatuses=truth_statuses or None,
            fetch=fetch,
            evidenceLimit=evidence_limit,
        )
        rows = self._read(statement, params, connection, max_rows=fetch).rows
        truncated = len(rows) > limit
        rows = rows[:limit]
        sources = self._sources(
            {item["sourceId"] for row in rows for item in row["evidence"]}, connection
        )
        return {
            "results": [self._assertion(row, sources) for row in rows],
            "truncated": truncated,
        }

    def find_connections(
        self,
        source: str,
        target: str,
        *,
        max_hops: int = 3,
        limit: int = 3,
        campaign_id: str | None = None,
        connection: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= max_hops <= 4:
            raise ValueError("max_hops must be between 1 and 4")
        policy = self._policy(campaign_id=campaign_id)
        start, start_resolution = self.resolve_entity(source, policy, connection)
        end, end_resolution = self.resolve_entity(target, policy, connection)
        if start["id"] == end["id"]:
            raise ValueError("source and target resolve to the same entity")
        # Each hop is Entity<-SUBJECT|OBJECT-Assertion-SUBJECT|OBJECT->Entity, so the
        # relationship bound is twice the hop count. Only permitted assertions may
        # be traversed; entities reached that way are visible by construction.
        statement = f"""
        MATCH (start:Entity {{id: $startId}}), (end:Entity {{id: $endId}})
        MATCH p = allShortestPaths((start)-[:SUBJECT|OBJECT*..{max_hops * 2}]-(end))
        WHERE all(n IN nodes(p) WHERE NOT n:Assertion OR ({assertion_filter("n")}))
        RETURN [n IN nodes(p) | n.id] AS nodeIds
        LIMIT $limit
        """
        rows = self._read(
            statement,
            self._params(policy, startId=start["id"], endId=end["id"], limit=limit),
            connection,
            max_rows=limit,
        ).rows
        assertion_ids = sorted(
            {row["nodeIds"][i] for row in rows for i in range(1, len(row["nodeIds"]), 2)}
        )
        details = {
            item["id"]: item
            for item in self._assertions(
                policy,
                connection,
                assertion_ids=assertion_ids,
                limit=max(len(assertion_ids), 1),
                evidence_limit=1,
            )["results"]
        }
        paths = []
        for row in rows:
            node_ids = row["nodeIds"]
            steps = []
            for index in range(1, len(node_ids), 2):
                item = details.get(node_ids[index])
                if item is None:
                    break
                forward = item["subject"]["id"] == node_ids[index - 1]
                steps.append(
                    {
                        "from": item["subject"] if forward else item["object"],
                        "to": item["object"] if forward else item["subject"],
                        "predicate": item["predicate"],
                        "direction": "forward" if forward else "reverse",
                        "statement": item["statement"],
                        "assertion_id": item["id"],
                        "truth_status": item["truth_status"],
                        "review_status": item["review_status"],
                        "evidence": item["evidence"][:1],
                    }
                )
            else:
                paths.append({"hops": len(steps), "steps": steps})
        return {
            "source": {"id": start["id"], "name": start["name"], "type": _type(start)},
            "target": {"id": end["id"], "name": end["name"], "type": _type(end)},
            "resolution": {"source": start_resolution, "target": end_resolution},
            "paths": paths,
            "note": None if paths else f"No permitted assertion path within {max_hops} hops.",
        }

    # ----------------------------------------------------------------- passages

    def search_passages(
        self,
        query: str,
        *,
        mode: str = "auto",
        limit: int = 8,
        campaign_id: str | None = None,
        canon_scopes: list[str] | None = None,
        source_codes: list[str] | None = None,
        content_types: list[str] | None = None,
        include_superseded: bool = False,
        max_chars: int = 1500,
        connection: str | None = None,
    ) -> dict[str, Any]:
        if mode not in {"auto", "fulltext", "vector", "hybrid"}:
            raise ValueError("mode must be auto, fulltext, vector, or hybrid")
        policy = self._policy(campaign_id=campaign_id, canon_scopes=canon_scopes)
        capabilities = self.capabilities(connection)
        notes: list[str] = []
        vector_ok = capabilities["vector_search"]
        if mode == "auto":
            mode = "hybrid" if vector_ok else "fulltext"
        if mode in {"vector", "hybrid"} and not vector_ok:
            reason = capabilities["vector_search_unavailable_reason"]
            if mode == "vector":
                raise ValueError(f"Vector search is unavailable: {reason}")
            notes.append(f"Vector search skipped ({reason}); used full-text only.")
            mode = "fulltext"
        # Filtering happens after index recall, so over-fetch to keep recall.
        fetch = min(max(limit * 6, 50), 500)
        extra = """
          AND ($sourceCodes IS NULL OR EXISTS {
                MATCH (v:MaterialVersion {id: c.sourceId}) WHERE v.code IN $sourceCodes })
          AND ($contentTypes IS NULL OR c.contentType IN $contentTypes)
        """
        base = self._params(
            policy,
            include_superseded,
            sourceCodes=source_codes or None,
            contentTypes=content_types or None,
            fetch=fetch,
            limit=limit,
        )
        rankings: dict[str, list[str]] = {}
        native_scores: dict[str, dict[str, float]] = {}
        if mode in {"fulltext", "hybrid"}:
            rows = self._read(
                f"""
                CALL db.index.fulltext.queryNodes($index, $lucene, {{limit: $fetch}})
                YIELD node AS c, score
                WHERE c:KnowledgeChunk AND {chunk_filter("c")} {extra}
                RETURN c.id AS id, score ORDER BY score DESC LIMIT $limit
                """,
                base | {"index": self.settings.chunk_index, "lucene": lucene_query(query)},
                connection,
            ).rows
            rankings["fulltext"] = [row["id"] for row in rows]
            native_scores["fulltext"] = {row["id"]: row["score"] for row in rows}
        vector: list[float] | None = None
        if mode in {"vector", "hybrid"}:
            assert self.embedder is not None
            try:
                vector = self.embedder.embed(query)
            except Exception as exc:  # a missing model must not break full-text recall
                if mode == "vector":
                    raise RuntimeError(f"Query embedding failed: {exc}") from exc
                notes.append(f"Vector search skipped (query embedding failed: {exc}).")
                mode = "fulltext"
            expected = capabilities["vector_index"]["dimensions"]
            if vector is not None and expected and int(expected) != len(vector):
                raise ValueError(
                    f"Query embedding has {len(vector)} dimensions but the index expects {expected}"
                )
        if vector is not None:
            rows = self._read(
                f"""
                CALL db.index.vector.queryNodes($index, $fetch, $vector)
                YIELD node AS c, score
                WHERE {chunk_filter("c")} {extra}
                RETURN c.id AS id, score ORDER BY score DESC LIMIT $limit
                """,
                base | {"index": self.settings.embeddings.vector_index_name, "vector": vector},
                connection,
            ).rows
            rankings["vector"] = [row["id"] for row in rows]
            native_scores["vector"] = {row["id"]: row["score"] for row in rows}
        if len(rankings) == 1:
            ((ranker, ids),) = rankings.items()
            ranked = [(item, round(native_scores[ranker][item], 4), [ranker]) for item in ids]
        else:
            ranked = reciprocal_rank_fusion(rankings)[:limit]
        passages = self._hydrate(
            [item for item, _, _ in ranked], policy, include_superseded, max_chars, connection
        )
        results = []
        for item, score, matched_by in ranked:
            if item in passages:
                results.append({**passages[item], "score": score, "matched_by": matched_by})
        return {"query": query, "mode": mode, "results": results, "notes": notes}

    def get_passage(
        self,
        chunk_id: str,
        *,
        neighbors: int = 1,
        max_chars: int = 6000,
        connection: str | None = None,
    ) -> dict[str, Any]:
        if not 0 <= neighbors <= 3:
            raise ValueError("neighbors must be between 0 and 3")
        policy = self._policy()
        params = self._params(policy, True, id=chunk_id)
        # Superseded chunks stay readable by id so old citations still resolve.
        if neighbors:
            statement = f"""
            MATCH (c:KnowledgeChunk {{id: $id}}) WHERE {chunk_filter("c")}
            OPTIONAL MATCH pa = (c)-[:NEXT_CHUNK*1..{neighbors}]->(after:KnowledgeChunk)
            WHERE {chunk_filter("after")}
            WITH c, after, length(pa) AS distance ORDER BY distance
            WITH c, collect(after.id) AS afterIds
            OPTIONAL MATCH pb = (before:KnowledgeChunk)-[:NEXT_CHUNK*1..{neighbors}]->(c)
            WHERE {chunk_filter("before")}
            WITH c, afterIds, before, length(pb) AS distance ORDER BY distance DESC
            RETURN c.id AS id, collect(before.id) AS beforeIds, afterIds
            """
        else:
            statement = f"""
            MATCH (c:KnowledgeChunk {{id: $id}}) WHERE {chunk_filter("c")}
            RETURN c.id AS id, [] AS beforeIds, [] AS afterIds
            """
        rows = self._read(statement, params, connection).rows
        if not rows:
            raise NotVisibleError(
                f"Passage {chunk_id!r} was not found or is not visible under the current access policy"
            )
        order = [*rows[0]["beforeIds"], chunk_id, *rows[0]["afterIds"]]
        passages = self._hydrate(order, policy, True, max_chars, connection)
        output = []
        for item in order:
            if item in passages:
                role = (
                    "target"
                    if item == chunk_id
                    else ("before" if item in rows[0]["beforeIds"] else "after")
                )
                output.append({"role": role, **passages[item]})
        return {"chunk_id": chunk_id, "passages": output}

    def _hydrate(
        self,
        chunk_ids: list[str],
        policy: AccessPolicy,
        include_superseded: bool,
        max_chars: int,
        connection: str | None,
    ) -> dict[str, dict[str, Any]]:
        if not chunk_ids:
            return {}
        statement = f"""
        UNWIND $ids AS chunkId
        MATCH (c:KnowledgeChunk {{id: chunkId}})
        WHERE {chunk_filter("c")}
        RETURN {_CHUNK_FIELDS},
          [(c)-[:MENTIONS]->(e:Entity)
             WHERE e.campaignId IS NULL OR size($campaignIds) = 0 OR e.campaignId IN $campaignIds
             | {{id: e.id, name: e.name, types: [label IN labels(e) WHERE label <> 'Entity']}}] AS mentions,
          [(c)-[:NEXT_CHUNK]->(n:KnowledgeChunk) WHERE {chunk_filter("n")} | n.id][0] AS nextId,
          [(p:KnowledgeChunk)-[:NEXT_CHUNK]->(c) WHERE {chunk_filter("p")} | p.id][0] AS previousId
        """
        rows = self._read(
            statement,
            self._params(policy, include_superseded, ids=list(dict.fromkeys(chunk_ids))),
            connection,
            max_rows=len(chunk_ids),
        ).rows
        sources = self._sources({row["chunk"]["sourceId"] for row in rows}, connection)
        output = {}
        for row in rows:
            chunk = row["chunk"]
            text, was_truncated = truncate(chunk["text"], max_chars)
            mentions = list({item["id"]: item for item in row["mentions"]}.values())
            output[chunk["id"]] = {
                **self._passage_header(chunk, sources),
                "text": text,
                "truncated": was_truncated,
                "content_type": chunk["contentType"],
                "profile": chunk["profile"],
                "visibility": chunk["visibility"],
                "canon_scope": chunk["canonScope"],
                "actuality": chunk["actuality"],
                "campaign_id": chunk["campaignId"],
                "status": chunk["status"],
                "mentions": [
                    {"id": item["id"], "name": item["name"], "type": _first(item["types"])}
                    for item in mentions[:20]
                ],
                "previous_chunk_id": row["previousId"],
                "next_chunk_id": row["nextId"],
            }
        return output

    def _passage_header(
        self, chunk: dict[str, Any], sources: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        source = sources.get(chunk["sourceId"])
        pages = page_label(chunk.get("pageStart"), chunk.get("pageEnd"))
        heading = chunk.get("headingPath") or None
        return {
            "chunk_id": chunk["id"],
            "citation": citation(source, pages, heading),
            "source_code": (source or {}).get("code"),
            "pages": pages,
            "heading": heading,
        }

    # ------------------------------------------------------------------ sources

    def list_sources(
        self,
        *,
        source_kinds: list[str] | None = None,
        campaign_id: str | None = None,
        include_superseded: bool = False,
        limit: int = 50,
        connection: str | None = None,
    ) -> dict[str, Any]:
        policy = self._policy(campaign_id=campaign_id)
        statement = f"""
        MATCH (v:MaterialVersion)
        WHERE v.visibility IN $visibilities
          AND ($includeSuperseded OR v.current = true)
          AND (size($canonScopes) = 0 OR v.canonScope IN $canonScopes)
          AND (v.campaignId IS NULL OR size($campaignIds) = 0 OR v.campaignId IN $campaignIds)
          AND ($sourceKinds IS NULL OR v.sourceKind IN $sourceKinds)
        WITH v ORDER BY v.code, v.edition LIMIT $fetch
        RETURN v.id AS id,
          COUNT {{ MATCH (c:KnowledgeChunk {{sourceId: v.id}}) WHERE {chunk_filter("c")} }} AS chunks
        """
        rows = self._read(
            statement,
            self._params(
                policy, include_superseded, sourceKinds=source_kinds or None, fetch=limit + 1
            ),
            connection,
            max_rows=limit + 1,
        ).rows
        sources = self._sources({row["id"] for row in rows[:limit]}, connection)
        results = []
        for row in rows[:limit]:
            source = sources.get(row["id"])
            if source:
                results.append({**source, "chunks": row["chunks"]})
        return {"results": results, "truncated": len(rows) > limit}

    def _sources(self, ids: set[str], connection: str | None) -> dict[str, dict[str, Any]]:
        ids = {item for item in ids if item}
        if not ids:
            return {}
        rows = self._read(
            """
            UNWIND $ids AS sourceId
            MATCH (v:MaterialVersion {id: sourceId})
            RETURN v.id AS id, v.code AS code,
                   coalesce([(m:Material)-[:HAS_MATERIAL_VERSION]->(v) | m.name][0], v.name) AS title,
                   v.edition AS edition, v.sourceKind AS source_kind, v.canonScope AS canon_scope,
                   v.actuality AS actuality, v.visibility AS visibility,
                   v.indexingMode AS indexing_mode, v.attribution AS attribution,
                   v.campaignId AS campaign_id, v.rulesetVersion AS ruleset_version,
                   v.current AS current, v.pageCount AS page_count
            """,
            {"ids": sorted(ids)},
            connection,
            max_rows=len(ids),
        ).rows
        return {row["id"]: row for row in rows}

    # ------------------------------------------------------------------- schema

    def graph_schema(self, connection: str | None = None) -> dict[str, Any]:
        policy = self._policy()
        params = self._params(policy)
        entity_types = self._read(
            f"""
            MATCH (e:Entity) WHERE {entity_filter("e")}
            UNWIND [label IN labels(e) WHERE label <> 'Entity'] AS type
            RETURN type, count(*) AS count ORDER BY count DESC, type
            """,
            params,
            connection,
        ).rows
        predicates = self._read(
            f"""
            MATCH (a:Assertion) WHERE {assertion_filter("a")}
            RETURN a.predicateCode AS predicate, count(*) AS count,
                   collect(DISTINCT a.reviewStatus) AS review_statuses
            ORDER BY count DESC, predicate
            """,
            params,
            connection,
        ).rows
        for row in predicates:
            rule = (self._schema or {}).get("predicates", {}).get(row["predicate"])
            if rule:
                row["domain"] = rule.get("domain")
                row["range"] = rule.get("range")
        campaigns = self._read(
            f"""
            MATCH (c:KnowledgeChunk) WHERE {chunk_filter("c")} AND c.campaignId IS NOT NULL
            RETURN c.campaignId AS campaign_id, count(*) AS chunks ORDER BY campaign_id
            """,
            params,
            connection,
        ).rows
        counts = self._read(
            f"""
            OPTIONAL MATCH (c:KnowledgeChunk) WHERE {chunk_filter("c")}
            WITH count(c) AS chunks
            OPTIONAL MATCH (a:Assertion) WHERE {assertion_filter("a")}
            WITH chunks, count(a) AS assertions
            OPTIONAL MATCH (v:MaterialVersion)
            WHERE v.visibility IN $visibilities AND v.current = true
              AND (size($canonScopes) = 0 OR v.canonScope IN $canonScopes)
              AND (v.campaignId IS NULL OR size($campaignIds) = 0 OR v.campaignId IN $campaignIds)
            RETURN chunks, assertions, count(v) AS sources
            """,
            params,
            connection,
        ).rows[0]
        output: dict[str, Any] = {
            "policy": policy.to_dict(),
            "visible_counts": counts,
            "entity_types": entity_types,
            "predicates": predicates,
            "campaigns": campaigns,
            "data_dictionary": DATA_DICTIONARY,
        }
        if self.settings.allow_raw_cypher:
            output["database_labels"] = [
                row["label"]
                for row in self._read(
                    "CALL db.labels() YIELD label RETURN label ORDER BY label", {}, connection
                ).rows
            ]
            output["relationship_types"] = [
                row["relationshipType"]
                for row in self._read(
                    "CALL db.relationshipTypes() YIELD relationshipType "
                    "RETURN relationshipType ORDER BY relationshipType",
                    {},
                    connection,
                ).rows
            ]
        return output

    # ---------------------------------------------------------------- raw read

    def read_cypher(
        self,
        query: str,
        *,
        parameters: dict[str, Any] | None = None,
        limit: int = 100,
        max_chars: int = 60_000,
        connection: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.allow_raw_cypher:
            raise PermissionError("Raw Cypher is disabled for this server")
        text = check_raw_cypher(query)
        query_type = self.gateway.query_type(text, parameters, connection=connection)
        if query_type != "r":
            raise PermissionError(
                f"Only read queries are allowed (planner reported {query_type!r})"
            )
        result = self._read(text, parameters or {}, connection, max_rows=limit)
        rows, truncated = result.rows, result.truncated
        while rows and len(json.dumps(rows, default=str)) > max_chars:
            rows = rows[: max(len(rows) // 2, 1)] if len(rows) > 1 else []
            truncated = True
        return {"rows": rows, "row_count": len(rows), "truncated": truncated}

    # ------------------------------------------------------------------ shaping

    def _entity(
        self, row: dict[str, Any], policy: AccessPolicy, *, full: bool = False
    ) -> dict[str, Any]:
        output: dict[str, Any] = {
            "id": row["id"],
            "name": row["name"],
            "type": _type(row),
            "subtype": row.get("subtype"),
            "aliases": row.get("aliases") or [],
            "campaign_id": row.get("campaignId"),
            "provisional": row.get("provisional"),
        }
        if len(row.get("types") or []) > 1:
            output["labels"] = row["types"]
        if full:
            output["scope"] = row.get("scope")
            output["confidence"] = row.get("confidence")
            # Entity text has no visibility of its own; only reveal it when the
            # policy cannot hide anything.
            if policy.unrestricted:
                output["description"] = row.get("description") or None
                structured = row.get("structuredData")
                if structured and structured != "{}":
                    try:
                        output["structured_data"] = json.loads(structured)
                    except (TypeError, ValueError):
                        output["structured_data"] = structured
            else:
                output["description"] = "[withheld: entity-level text has no visibility label]"
        return output

    def _assertion(self, row: dict[str, Any], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
        assertion = row["assertion"]
        subject = _named(row["subject"])
        obj = _named(row["object"]) if row["object"] else None
        target = obj["name"] if obj else repr(row["literal"])
        evidence = []
        for item in row["evidence"]:
            source = sources.get(item["sourceId"])
            pages = page_label(item.get("pageStart"), item.get("pageEnd"))
            evidence.append(
                {
                    "quote": item["quote"],
                    "citation": citation(source, pages, item.get("heading") or None),
                    "source_code": (source or {}).get("code"),
                    "pages": pages,
                    "chunk_id": item["chunkId"],
                    "fragment_id": item["fragmentId"],
                }
            )
        return {
            "id": assertion["id"],
            "statement": f"{subject['name']} {assertion['predicateCode']} {target}",
            "subject": subject,
            "predicate": assertion["predicateCode"],
            "object": obj,
            "literal": row["literal"],
            "truth_status": assertion["truthStatus"],
            "review_status": assertion["reviewStatus"],
            "actuality": assertion["actuality"],
            "canon_scope": assertion["canonScope"],
            "visibility": assertion["visibility"],
            "campaign_id": assertion["campaignId"],
            "confidence": assertion["confidence"],
            "derivation_method": assertion["derivationMethod"],
            "valid_from": assertion["validFrom"],
            "valid_to": assertion["validTo"],
            "active": assertion["active"],
            "asserted_by": row["assertedBy"],
            "evidence": evidence,
        }


def _type(row: dict[str, Any]) -> str | None:
    return _first(row.get("types") or [])


def _first(values: list[str]) -> str | None:
    return min(values) if values else None


def _named(value: dict[str, Any]) -> dict[str, Any]:
    return {"id": value["id"], "name": value["name"], "type": _first(value.get("types") or [])}


def _load_schema(path: Path | None) -> dict[str, Any] | None:
    if path is None or not Path(path).is_file():
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

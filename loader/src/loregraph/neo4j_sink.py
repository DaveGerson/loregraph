from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .audit import audit_store
from .config import LoaderConfig
from .ids import stable_id
from .schema import SchemaRegistry
from .store import RecordStore


def load_neo4j(config: LoaderConfig) -> dict[str, int]:
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("Install the Neo4j extra: pip install -e '.[neo4j]'") from exc

    audit = audit_store(config)
    if not audit.ok:
        raise ValueError(f"Ledger audit failed; refusing Neo4j load: {audit.errors[:10]}")
    credentials = config.neo4j_credentials
    if not credentials["uri"] or not credentials["password"]:
        raise RuntimeError("NEO4J_URI and NEO4J_PASSWORD must be configured")
    store = RecordStore(config.workspace_dir)
    schema = SchemaRegistry(config.schema_path)
    load_id = uuid4().hex
    counts: dict[str, int] = {}
    with GraphDatabase.driver(
        credentials["uri"], auth=(credentials["username"], credentials["password"])
    ) as driver:
        driver.verify_connectivity()
        with driver.session(database=credentials["database"]) as session:
            _apply_schema(session)
            sources = store.read("sources")
            _write_sources(session, sources, config.neo4j.batch_size)
            counts["sources"] = len(sources)
            chunks = store.read("chunks")
            _write_chunks(session, chunks, config.neo4j.batch_size)
            counts["chunks"] = len(chunks)
            entities = store.read("entities")
            _write_entities(session, entities, schema, config.neo4j.batch_size)
            counts["entities"] = len(entities)
            mentions = store.read("mentions")
            _write_mentions(session, mentions, config.neo4j.batch_size)
            counts["mentions"] = len(mentions)
            evidence = store.read("evidence")
            _write_evidence(session, evidence, config.neo4j.batch_size)
            counts["evidence"] = len(evidence)
            embeddings = store.read("embeddings")
            _clear_chunk_embeddings(session, chunks, config.neo4j.batch_size)
            _write_embeddings(session, embeddings, config)
            counts["embeddings"] = len(embeddings)
            assertions = store.read("assertions")
            current_sources = {
                row["id"] for row in sources if row.get("metadata", {}).get("is_current", True)
            }
            current_chunks = {row["id"] for row in chunks if row["source_id"] in current_sources}
            assertion_rows = []
            for row in assertions:
                copy = dict(row)
                provenance = row.get("source_chunk_ids", [])
                copy["active"] = not provenance or bool(current_chunks.intersection(provenance))
                assertion_rows.append(copy)
            _write_assertions(session, assertion_rows, config.neo4j.batch_size)
            _touch_and_sweep_managed(
                session,
                load_id,
                sources,
                chunks,
                entities,
                evidence,
                embeddings,
                assertions,
                config.neo4j.batch_size,
            )
            if config.neo4j.create_projection_edges:
                session.run(
                    "MATCH ()-[r]->() WHERE r.projection = true "
                    "AND r.loaderNamespace = 'loregraph' DELETE r"
                ).consume()
                _write_projection_edges(
                    session,
                    assertion_rows,
                    schema,
                    config.neo4j.batch_size,
                    config.neo4j.project_unreviewed,
                )
            counts["assertions"] = len(assertions)
    return counts


def _apply_schema(session: Any) -> None:
    path = Path(__file__).resolve().parents[2] / "schema" / "neo4j.cypher"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("//")]
    for statement in "\n".join(lines).split(";"):
        if statement.strip():
            session.run(statement.strip()).consume()


def _write_sources(session: Any, rows: list[dict[str, Any]], batch_size: int) -> None:
    query = """
    UNWIND $rows AS row
    MERGE (m:Entity:Material {id: row.material_id})
      SET m.name = row.title, m.slug = row.code, m.updatedAt = datetime()
    MERGE (v:Entity:MaterialVersion {id: row.id})
      SET v.name = row.title + ' (' + row.edition + ')',
          v.code = row.code, v.edition = row.edition, v.fileHash = row.file_hash,
          v.sourceKind = row.source_kind, v.canonScope = row.canon_scope,
          v.actuality = row.actuality, v.rightsBasis = row.rights_basis,
          v.indexingMode = row.indexing_mode, v.visibility = row.visibility,
          v.attribution = row.attribution,
          v.gameSystem = row.game_system,
          v.ruleset = row.ruleset,
          v.rulesetVersion = row.ruleset_version,
          v.remoteProcessingAllowed = row.remote_processing_allowed,
          v.parser = row.parser,
          v.campaignId = row.campaign_id, v.pageCount = row.page_count,
          v.current = coalesce(row.metadata.is_current, false), v.updatedAt = datetime()
    MERGE (m)-[:HAS_MATERIAL_VERSION]->(v)
    """
    _batches(session, query, rows, batch_size)
    current = [row for row in rows if row.get("metadata", {}).get("is_current")]
    current_query = """
    UNWIND $rows AS row
    MATCH (m:Material {id: row.material_id}), (v:MaterialVersion {id: row.id})
    OPTIONAL MATCH (m)-[old:CURRENT_VERSION]->(:MaterialVersion)
    DELETE old
    MERGE (m)-[:CURRENT_VERSION]->(v)
    """
    _batches(session, current_query, current, batch_size)


def _write_chunks(session: Any, rows: list[dict[str, Any]], batch_size: int) -> None:
    prepared = []
    for row in rows:
        copy = dict(row)
        copy["fragment_id"] = stable_id("global", "fragment", row["id"], "retrieval-unit")
        copy["heading_text"] = " > ".join(row.get("heading_path", []))
        prepared.append(copy)
    query = """
    UNWIND $rows AS row
    MATCH (v:MaterialVersion {id: row.source_id})
    MERGE (f:Entity:SourceFragment {id: row.fragment_id})
      SET f.name = row.heading_text, f.pageStart = row.page_start,
          f.pageEnd = row.page_end, f.locator = 'pp. ' + toString(row.page_start) +
            CASE WHEN row.page_end <> row.page_start THEN '-' + toString(row.page_end) ELSE '' END,
          f.materialVersionId = row.source_id
    MERGE (f)-[:FROM_MATERIAL_VERSION]->(v)
    MERGE (c:KnowledgeChunk {id: row.id})
      SET c.sourceId = row.source_id, c.chunkIndex = row.chunk_index,
          c.text = row.text, c.rawText = row.raw_text, c.textHash = row.text_hash,
          c.tokenCount = row.token_count, c.pageStart = row.page_start,
          c.pageEnd = row.page_end, c.headingPath = row.heading_text,
          c.contentType = row.content_type, c.profile = row.profile,
          c.actuality = row.actuality, c.canonScope = row.canon_scope,
          c.campaignId = row.campaign_id,
          c.gameSystem = row.game_system,
          c.ruleset = row.ruleset,
          c.rulesetVersion = row.ruleset_version,
          c.visibility = row.visibility, c.indexingMode = row.indexing_mode,
          c.remoteProcessingAllowed = row.remote_processing_allowed,
          c.status = CASE WHEN v.current THEN 'ACTIVE' ELSE 'SUPERSEDED' END
    MERGE (c)-[:FROM_FRAGMENT]->(f)
    """
    _batches(session, query, prepared, batch_size)
    link_rows = [
        {"id": row["id"], "next": row.get("next_chunk_id")}
        for row in rows
        if row.get("next_chunk_id")
    ]
    link_query = """
    UNWIND $rows AS row
    MATCH (a:KnowledgeChunk {id: row.id}), (b:KnowledgeChunk {id: row.next})
    MERGE (a)-[:NEXT_CHUNK]->(b)
    """
    _batches(session, link_query, link_rows, batch_size)


def _write_entities(
    session: Any,
    rows: list[dict[str, Any]],
    schema: SchemaRegistry,
    batch_size: int,
) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["entity_type"] not in schema.node_types:
            continue
        copy = dict(row)
        copy["aliases_text"] = " | ".join(row.get("aliases", []))
        copy["structured_json"] = json.dumps(
            row.get("metadata", {}).get("structured", {}), ensure_ascii=False, sort_keys=True
        )
        grouped[row["entity_type"]].append(copy)
    for label, group in grouped.items():
        query = f"""
        UNWIND $rows AS row
        MERGE (e:Entity:`{label}` {{id: row.id}})
          SET e.name = row.canonical_name, e.subtype = row.subtype,
              e.aliases = row.aliases, e.aliasesText = row.aliases_text,
              e.scope = row.scope, e.description = row.description,
              e.campaignId = row.campaign_id,
              e.confidence = row.confidence, e.provisional = row.provisional,
              e.structuredData = row.structured_json,
              e.updatedAt = datetime()
        """
        _batches(session, query, group, batch_size)

    names = []
    for row in rows:
        for index, value in enumerate([row["canonical_name"], *row.get("aliases", [])]):
            names.append(
                {
                    "entity_id": row["id"],
                    "id": stable_id("global", "lexical-entry", row["id"], value),
                    "value": value,
                    "name_type": "PREFERRED" if index == 0 else "ALIAS",
                    "preferred": index == 0,
                }
            )
    name_query = """
    UNWIND $rows AS row
    MATCH (e:Entity {id: row.entity_id})
    MERGE (n:Entity:LexicalEntry {id: row.id})
      SET n.name = row.value, n.value = row.value, n.nameType = row.name_type,
          n.preferred = row.preferred, n.language = 'en'
    MERGE (e)-[:HAS_NAME]->(n)
    """
    _batches(session, name_query, names, batch_size)


def _write_mentions(session: Any, rows: list[dict[str, Any]], batch_size: int) -> None:
    query = """
    UNWIND $rows AS row
    MATCH (c:KnowledgeChunk {id: row.chunk_id}), (e:Entity {id: row.entity_id})
    MERGE (c)-[m:MENTIONS {mentionId: row.id}]->(e)
      SET m.surface = row.surface, m.start = row.start, m.end = row.end,
          m.method = row.method, m.confidence = row.confidence
    """
    _batches(session, query, rows, batch_size)


def _write_evidence(session: Any, rows: list[dict[str, Any]], batch_size: int) -> None:
    query = """
    UNWIND $rows AS row
    MATCH (v:MaterialVersion {id: row.source_id}), (c:KnowledgeChunk {id: row.chunk_id})
    MERGE (f:Entity:SourceFragment {id: row.id})
      SET f.name = left(row.quote, 100), f.selectorType = 'TEXT_QUOTE',
          f.selectorValue = row.quote, f.exactTextHash = row.exact_text_hash,
          f.pageStart = row.page_start, f.pageEnd = row.page_end,
          f.materialVersionId = row.source_id
    MERGE (f)-[:FROM_MATERIAL_VERSION]->(v)
    MERGE (f)-[:EVIDENCE_IN_CHUNK]->(c)
    """
    _batches(session, query, rows, batch_size)


def _write_assertions(session: Any, rows: list[dict[str, Any]], batch_size: int) -> None:
    prepared = []
    for row in rows:
        copy = dict(row)
        copy["literal_id"] = (
            stable_id("global", "literal", row["id"], row["literal_value"])
            if row.get("literal_value") is not None
            else None
        )
        prepared.append(copy)
    query = """
    UNWIND $rows AS row
    MATCH (s:Entity {id: row.subject_id})
    MERGE (a:Entity:Assertion {id: row.id})
      SET a.subjectId = row.subject_id, a.predicateCode = row.predicate_code,
          a.truthStatus = row.truth_status, a.canonScope = row.canon_scope,
          a.actuality = row.actuality, a.campaignId = row.campaign_id,
          a.confidence = row.confidence,
          a.reviewStatus = row.review_status, a.derivationMethod = row.derivation_method,
          a.validFrom = row.valid_from, a.validTo = row.valid_to,
          a.visibility = row.visibility, a.active = row.active,
          a.recordedAt = datetime()
    MERGE (a)-[:SUBJECT]->(s)
    FOREACH (_ IN CASE WHEN row.literal_id IS NOT NULL THEN [1] ELSE [] END |
      MERGE (l:Entity:LiteralValue {id: row.literal_id})
        SET l.name = row.literal_value, l.value = row.literal_value
      MERGE (a)-[:LITERAL_OBJECT]->(l))
    """
    _batches(session, query, prepared, batch_size)
    object_rows = [
        {"assertion_id": row["id"], "object_id": row["object_id"]}
        for row in rows
        if row.get("object_id")
    ]
    object_query = """
    UNWIND $rows AS row
    MATCH (a:Assertion {id: row.assertion_id}), (o:Entity {id: row.object_id})
    MERGE (a)-[:OBJECT]->(o)
    """
    _batches(session, object_query, object_rows, batch_size)
    support_rows = [
        {"assertion_id": row["id"], "evidence_id": evidence_id}
        for row in rows
        for evidence_id in row.get("evidence_ids", [])
    ]
    support_query = """
    UNWIND $rows AS row
    MATCH (a:Assertion {id: row.assertion_id}), (f:SourceFragment {id: row.evidence_id})
    MERGE (a)-[:SUPPORTED_BY]->(f)
    """
    _batches(session, support_query, support_rows, batch_size)
    author_rows = [
        {"assertion_id": row["id"], "author_id": row["asserted_by_id"]}
        for row in rows
        if row.get("asserted_by_id")
    ]
    author_query = """
    UNWIND $rows AS row
    MATCH (a:Assertion {id: row.assertion_id}), (author:Entity {id: row.author_id})
    MERGE (a)-[:ASSERTED_BY]->(author)
    """
    _batches(session, author_query, author_rows, batch_size)


def _write_embeddings(session: Any, rows: list[dict[str, Any]], config: LoaderConfig) -> None:
    query = """
    UNWIND $rows AS row
    MATCH (c:KnowledgeChunk {id: row.chunk_id})
    MERGE (e:Embedding {id: row.id})
      SET e.provider = row.provider, e.model = row.model,
          e.dimensions = row.dimensions, e.inputHash = row.input_hash,
          e.vector = row.vector, e.active = row.active
    MERGE (c)-[:HAS_EMBEDDING]->(e)
    FOREACH (_ IN CASE WHEN row.active THEN [1] ELSE [] END |
      SET c.embedding = row.vector,
          c.embeddingModel = row.model,
          c.embeddingDimensions = row.dimensions)
    """
    _batches(session, query, rows, config.neo4j.batch_size)
    active = [row for row in rows if row.get("active")]
    dimensions = {int(row["dimensions"]) for row in active}
    if not active:
        return
    if len(dimensions) != 1:
        raise ValueError("Active embeddings have mixed dimensions; choose one active embedding model")
    dimension = dimensions.pop()
    index_name = config.embeddings.vector_index_name
    existing = session.run(
        "SHOW INDEXES YIELD name, options WHERE name = $name RETURN options",
        name=index_name,
    ).single()
    if existing:
        index_config = dict((existing["options"] or {}).get("indexConfig", {}))
        configured_dimension = index_config.get("vector.dimensions")
        if configured_dimension is not None and int(configured_dimension) != dimension:
            session.run(f"DROP INDEX `{index_name}` IF EXISTS").consume()
    session.run(
        f"""
        CREATE VECTOR INDEX `{index_name}` IF NOT EXISTS
        FOR (n:KnowledgeChunk) ON n.embedding
        OPTIONS {{indexConfig: {{`vector.dimensions`: {dimension},
                               `vector.similarity_function`: 'cosine'}}}}
        """
    ).consume()


def _clear_chunk_embeddings(
    session: Any, rows: list[dict[str, Any]], batch_size: int
) -> None:
    query = """
    UNWIND $rows AS row
    MATCH (c:KnowledgeChunk {id: row.id})
    REMOVE c.embedding, c.embeddingModel, c.embeddingDimensions
    """
    _batches(session, query, [{"id": row["id"]} for row in rows], batch_size)


def _write_projection_edges(
    session: Any,
    rows: list[dict[str, Any]],
    schema: SchemaRegistry,
    batch_size: int,
    project_unreviewed: bool,
) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if (
            not row.get("active", True)
            or row.get("object_id") is None
            or row["predicate_code"] not in schema.predicates
        ):
            continue
        if row["review_status"] not in ({"ACCEPTED", "UNREVIEWED"} if project_unreviewed else {"ACCEPTED"}):
            continue
        grouped[row["predicate_code"]].append(row)
    for predicate, group in grouped.items():
        query = f"""
        UNWIND $rows AS row
        MATCH (s:Entity {{id: row.subject_id}}), (o:Entity {{id: row.object_id}})
        MERGE (s)-[r:`{predicate}` {{assertionId: row.id}}]->(o)
          SET r.projection = true, r.confidence = row.confidence,
              r.loaderNamespace = 'loregraph',
              r.reviewStatus = row.review_status, r.canonScope = row.canon_scope,
              r.actuality = row.actuality, r.campaignId = row.campaign_id,
              r.validFrom = row.valid_from,
              r.validTo = row.valid_to
        """
        _batches(session, query, group, batch_size)


def _touch_and_sweep_managed(
    session: Any,
    load_id: str,
    sources: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    embeddings: list[dict[str, Any]],
    assertions: list[dict[str, Any]],
    batch_size: int,
) -> None:
    ids: set[str] = {row["id"] for row in entities}
    ids.update(row["material_id"] for row in sources)
    ids.update(row["id"] for row in sources)
    ids.update(row["id"] for row in chunks)
    ids.update(stable_id("global", "fragment", row["id"], "retrieval-unit") for row in chunks)
    ids.update(row["id"] for row in evidence)
    ids.update(row["id"] for row in embeddings)
    ids.update(row["id"] for row in assertions)
    ids.update(
        stable_id("global", "literal", row["id"], row["literal_value"])
        for row in assertions
        if row.get("literal_value") is not None
    )
    ids.update(
        stable_id("global", "lexical-entry", row["id"], value)
        for row in entities
        for value in [row["canonical_name"], *row.get("aliases", [])]
    )
    touch_query = """
    UNWIND $rows AS row
    MATCH (n {id: row.id})
    SET n.loaderManaged = true, n.loaderNamespace = 'loregraph',
        n.loaderLoadId = $load_id
    """
    rows = [{"id": value} for value in sorted(ids)]
    for start in range(0, len(rows), batch_size):
        session.run(
            touch_query,
            rows=rows[start : start + batch_size],
            load_id=load_id,
        ).consume()
    session.run(
        """
        MATCH (n)
        WHERE n.loaderManaged = true AND n.loaderNamespace = 'loregraph'
          AND n.loaderLoadId <> $load_id
        DETACH DELETE n
        """,
        load_id=load_id,
    ).consume()


def _batches(session: Any, query: str, rows: list[dict[str, Any]], size: int) -> None:
    for start in range(0, len(rows), size):
        session.run(query, rows=rows[start : start + size]).consume()

// LoreGraph storage projection for Neo4j 5.18.1+ / Aura.
// Assertions are authoritative. Domain relationships are optional projections
// and always carry assertionId, confidence, reviewStatus, and projection=true.

CREATE CONSTRAINT entity_id_unique IF NOT EXISTS
FOR (n:Entity) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT material_id_unique IF NOT EXISTS
FOR (n:Material) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT material_version_id_unique IF NOT EXISTS
FOR (n:MaterialVersion) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT fragment_id_unique IF NOT EXISTS
FOR (n:SourceFragment) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS
FOR (n:KnowledgeChunk) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT assertion_id_unique IF NOT EXISTS
FOR (n:Assertion) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT lexical_entry_id_unique IF NOT EXISTS
FOR (n:LexicalEntry) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT embedding_id_unique IF NOT EXISTS
FOR (n:Embedding) REQUIRE n.id IS UNIQUE;

CREATE FULLTEXT INDEX entity_search IF NOT EXISTS
FOR (n:Entity) ON EACH [n.name, n.aliasesText, n.description];

CREATE FULLTEXT INDEX chunk_fulltext IF NOT EXISTS
FOR (n:KnowledgeChunk) ON EACH [n.text, n.headingPath];

CREATE INDEX assertion_subject_predicate IF NOT EXISTS
FOR (n:Assertion) ON (n.subjectId, n.predicateCode);

CREATE INDEX assertion_scope_review IF NOT EXISTS
FOR (n:Assertion) ON (n.canonScope, n.actuality, n.reviewStatus);

CREATE INDEX assertion_campaign_scope IF NOT EXISTS
FOR (n:Assertion) ON (n.campaignId, n.actuality, n.visibility);

CREATE INDEX chunk_source_order IF NOT EXISTS
FOR (n:KnowledgeChunk) ON (n.sourceId, n.chunkIndex);

// Create the vector index separately after choosing one fixed embedding model:
// CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS
// FOR (n:KnowledgeChunk) ON n.embedding
// OPTIONS {indexConfig: {`vector.dimensions`: 1536,
//                        `vector.similarity_function`: 'cosine'}};

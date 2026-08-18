// LoreGraph GraphRAG retrieval recipes.
// Parameterize these queries in an application; do not concatenate user input.

// 1. Entity-first retrieval: resolve a name, expand supported active assertions,
// and return exact evidence chunks. Add canon/visibility filters for the caller.
CALL db.index.fulltext.queryNodes('entity_search', $entityQuery, {limit: 8})
YIELD node AS seed, score AS entityScore
WHERE seed:Entity AND NOT seed:Assertion AND NOT seed:SourceFragment
MATCH (assertion:Assertion)-[:SUBJECT|OBJECT]-(seed)
WHERE assertion.active = true
  AND assertion.reviewStatus IN $allowedReviewStatuses
  AND assertion.visibility IN $allowedVisibilities
  AND assertion.canonScope IN $allowedCanonScopes
MATCH (assertion)-[:SUPPORTED_BY]->(fragment:SourceFragment)
MATCH (fragment)-[:EVIDENCE_IN_CHUNK]->(chunk:KnowledgeChunk)
WHERE chunk.status = 'ACTIVE'
RETURN seed.id, seed.name, entityScore,
       assertion.id, assertion.predicateCode, assertion.truthStatus,
       assertion.actuality, assertion.confidence,
       fragment.selectorValue AS exactQuote,
       fragment.pageStart, fragment.pageEnd,
       chunk.id AS chunkId, chunk.text AS context
ORDER BY entityScore DESC, assertion.confidence DESC
LIMIT $limit;

// 2. Vector-first retrieval with one-hop semantic expansion.
CALL db.index.vector.queryNodes('chunk_embedding', $k, $queryEmbedding)
YIELD node AS chunk, score
WHERE chunk.status = 'ACTIVE'
  AND chunk.visibility IN $allowedVisibilities
OPTIONAL MATCH (chunk)-[:MENTIONS]->(entity:Entity)
OPTIONAL MATCH (assertion:Assertion)-[:SUBJECT|OBJECT]-(entity)
WHERE assertion.active = true
  AND assertion.reviewStatus IN $allowedReviewStatuses
  AND assertion.canonScope IN $allowedCanonScopes
OPTIONAL MATCH (assertion)-[:SUPPORTED_BY]->(evidence:SourceFragment)
OPTIONAL MATCH (evidence)-[:EVIDENCE_IN_CHUNK]->(supporting:KnowledgeChunk)
WHERE supporting.status = 'ACTIVE'
RETURN chunk.id, chunk.text, score,
       collect(DISTINCT entity.name)[0..20] AS mentionedEntities,
       collect(DISTINCT {
         assertionId: assertion.id,
         predicate: assertion.predicateCode,
         truthStatus: assertion.truthStatus,
         actuality: assertion.actuality,
         quote: evidence.selectorValue,
         supportingChunkId: supporting.id
       })[0..30] AS graphEvidence
ORDER BY score DESC;

// 3. Campaign truth: occurred play only, excluding prep and authored adventures.
MATCH (assertion:Assertion {active: true, canonScope: 'CAMPAIGN', actuality: 'OCCURRED'})
WHERE assertion.reviewStatus IN $allowedReviewStatuses
  AND assertion.visibility IN $allowedVisibilities
  AND assertion.campaignId = $campaignId
MATCH (assertion)-[:SUBJECT]->(subject:Entity)
OPTIONAL MATCH (assertion)-[:OBJECT]->(object:Entity)
OPTIONAL MATCH (assertion)-[:SUPPORTED_BY]->(fragment:SourceFragment)
RETURN subject.name, assertion.predicateCode, object.name,
       assertion.truthStatus, fragment.selectorValue,
       fragment.pageStart, fragment.pageEnd
ORDER BY subject.name, assertion.predicateCode;

// 4. Rules lookup pinned to one immutable ruleset version.
MATCH (version:RuleElementVersion)-[:VERSION_OF]->(rule:RuleElement)
MATCH (version)-[:FOR_RULESET_VERSION]->(ruleset:RulesetVersion {id: $rulesetVersionId})
WHERE toLower(rule.name) CONTAINS toLower($ruleName)
OPTIONAL MATCH (assertion:Assertion {active: true})-[:SUBJECT|OBJECT]-(version)
OPTIONAL MATCH (assertion)-[:SUPPORTED_BY]->(fragment:SourceFragment)
RETURN rule.name, version.id, ruleset.name,
       collect(DISTINCT {predicate: assertion.predicateCode,
                         quote: fragment.selectorValue,
                         page: fragment.pageStart}) AS evidence;

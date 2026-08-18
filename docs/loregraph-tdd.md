# LoreGraph Technical Design Document

**Version:** 1.0  
**Status:** Implementation baseline  
**Meaning of TDD:** Technical Design Document

## 1. Objectives

Build a local-first ingestion and retrieval system that can support general
lore, tabletop campaign operations, and ordinary knowledge notes from one
provenance-preserving model. Neo4j Aura and Obsidian are independent projections
of a portable evidence ledger.

The design optimizes for correctness, inspectability, low recurring model cost,
and gradual adoption by one technically comfortable user.

## 2. Non-goals

- A universal ontology for every domain.
- Automatic legal classification of copyrighted sources.
- A virtual tabletop, campaign chat platform, or multiplayer sync service.
- Autonomous acceptance of extracted assertions.
- Storing the only copy of semantic truth in a vendor-specific database.

## 3. Architecture

```text
PDF / Markdown / document / note / webpage
  -> material registry + rights gate
  -> structural parser
  -> SourceFragment + deterministic KnowledgeChunk
  -> profile router
  -> deterministic extraction + optional LLM candidates
  -> conservative entity resolution
  -> validation + review queue
  -> Assertion/Evidence JSONL ledger
       |-> Neo4j graph + vector indexes
       |-> Obsidian vault + backlinks
       `-> application retrieval/context service
```

The ledger is the write model. Projections are read models. Projection writers
are idempotent and may be replayed after schema/index changes.

## 4. Repository boundaries

```text
schema/
  canonical-profile.json      closed types, predicates, profiles
  neo4j.cypher                 constraints and indexes
config/
  project.example.toml        parser, chunk, budget, sink settings
  sources.example.yaml        rights-aware source manifest
  backbone.empty.json         generic default identity pack
  backbone.eberron.json       optional validation identity pack
src/loregraph/
  parsers.py                   PDF/Markdown structural recovery
  chunking.py                  semantic boundaries and lineage
  classification.py           content/profile routing
  mechanics.py                deterministic structured extraction
  llm.py                      constrained candidate extraction
  resolution.py               stable identity matching
  validation.py               schema and grounding checks
  store.py                    JSONL ledger + SQLite stage state
  neo4j_sink.py               graph/vector projection
  obsidian_sink.py            vault projection
  audit.py                    integrity and review sampling
  pipeline.py                 idempotent orchestration
```

## 5. Canonical data model

### 5.1 Source chain

`Material` identifies the conceptual work. `MaterialVersion` identifies an
edition, file, release, or snapshot. `SourceFragment` is an addressable page,
section, quote, time range, or map region. `KnowledgeChunk` is a retrieval and
extraction unit derived from one or more fragments.

Required source-version fields:

- deterministic ID and material ID;
- source code/title/edition/kind;
- file hash and parser version;
- rights basis, indexing mode, attribution, visibility;
- hosted-processing permission;
- default canon/knowledge scope and actuality;
- optional campaign ID and ruleset version.

### 5.2 Identity

`Entity` is the conceptual superclass represented by typed records. Stable
identity is separate from names, aliases, mentions, rules versions, campaign
state, and source descriptions. IDs are deterministic when a trustworthy scope
and normalized key exist; otherwise candidates receive scoped provisional IDs.

### 5.3 Assertion

```text
Assertion
  id
  subjectId
  predicateCode
  objectId | literalValue
  evidenceIds[]
  sourceChunkIds[]
  truthStatus
  canonScope
  actuality
  visibility
  confidence
  reviewStatus
  derivationMethod
  assertedById?
  validFrom? / validTo?
  campaignId?
```

Assertions, not direct graph edges, are authoritative. Contradiction,
supersession, attribution, and perspective are represented explicitly. A
materialized direct relationship stores `assertionId` and can be regenerated.

### 5.4 Truth dimensions

Keep these independent:

- truth: asserted, disputed, unknown, false, or retracted;
- source/canon scope: setting, adventure, campaign, homebrew, reference,
  personal, or none;
- actuality: authored, planned, occurred, hypothetical, superseded;
- visibility: public, player, DM-only, private;
- review: unreviewed, accepted, rejected, needs review;
- valid time versus record time.

## 6. Ontology strategy

### 6.1 Generic kernel

The non-negotiable kernel is source/version/fragment/chunk, entity/alias,
assertion/predicate/literal, evidence/citation, access policy, revision, and
review. It has no Eberron or game-system dependency.

### 6.2 Closed profiles

Profiles reduce prompt size and prevent the model from inventing labels:

- `lore`: people/characters, factions, places, events, time, concepts, items;
- `lore_eberron`: lore plus optional Eberron-specific relationships;
- `rules`: system/ruleset versions, rule elements, stat blocks, item definitions;
- `adventure`: reusable scenes, encounters, narrative elements, tools;
- `campaign`: continuity, sessions, occurred runs, items, narrative state;
- `character`: identity, build, runtime state, inventory, rules pinning;
- `notes`: people, organizations, topics, projects, action items, decisions,
  questions, events, time, and location.

A source may explicitly select a profile. Otherwise the router derives one from
source kind and deterministic content classification.

### 6.3 Extension packs

A pack may provide identity/alias seeds, leaf subtypes, prompt examples, and
special predicates. Pack data cannot establish lore facts by itself. The first
pack is Eberron; later packs can target other settings, game systems, research
domains, or personal vocabularies.

## 7. Ingestion pipeline

### Stage 1 — Register and govern

Hash the input. Resolve or create Material/MaterialVersion. Validate source kind,
scope, rights/indexing mode, attribution, visibility, and remote permission.
`NO_INDEX` and `METADATA_ONLY` sources never produce unrestricted chunks.

### Stage 2 — Structural parsing

Use Docling when available for layout/OCR; otherwise use PyMuPDF for PDFs and a
Markdown parser for notes. Preserve page boundaries, heading paths, reading
order, table/stat-block atomicity, and excluded ranges.

### Stage 3 — Semantic chunking

Prefer heading/paragraph/list/table boundaries. Target roughly 700 tokens, cap
normal chunks near 950, allow larger atomic structures, and use zero extraction
overlap. Store previous/next chunk IDs so retrieval can recover local context
without paying to extract duplicated text.

Chunk IDs include source-version ID, parser/chunker fingerprint, ordered span,
and content hash. A changed configuration supersedes derived records but does
not erase addressable source evidence.

### Stage 4 — Deterministic enrichment

Run longest-alias identity-pack matching, headings, known rules/stat-block
parsers, date/dice normalization, and cheap metadata extraction. These results
are high-confidence but still carry derivation method and source chunk IDs.

### Stage 5 — Candidate extraction

Only chunks that pass all permission and budget gates are submitted. The model
receives the compact profile, candidate identities, source metadata, exact
chunk text, and strict JSON Schema. Relationships require an exact supporting
quote grounded in the chunk. Output is always `UNREVIEWED`.

### Stage 6 — Entity resolution

Resolution order:

1. deterministic declared ID;
2. exact normalized alias within compatible type and scope;
3. unique high-confidence fuzzy candidate after campaign/ruleset/location/time
   constraints;
4. optional adjudication for a small set of valuable ambiguous collisions;
5. provisional new identity plus review item.

Record-like objects such as item instances are not cross-chunk merged solely by
name. Campaign-scoped entities never merge across campaign IDs.

### Stage 7 — Validate and commit

Validate type/profile membership, predicate membership, domain/range, grounded
quotes, endpoint existence, evidence/source lineage, campaign/ruleset scope,
and vector dimensions. Quarantine invalid candidates. Commit valid records via
atomic file replacement and update stage fingerprints only after success.

### Stage 8 — Project

Neo4j projection uses `MERGE` on stable IDs, creates full-text/vector indexes,
stores reified assertions/evidence, and optionally materializes direct active
edges. Obsidian projection writes generated folders, YAML properties,
wikilinks, source notes, and optional evidence/assertion notes.

## 8. Retrieval design

```text
principal + question + selected workspace/campaign/ruleset
  -> deny-first rights/visibility/scope filters
  -> entity and temporal intent resolution
  -> lexical + vector candidate recall
  -> bounded assertion/entity neighborhood expansion
  -> evidence-quality and source-authority rerank
  -> context budget allocation
  -> cited answer with uncertainty structure
```

Hard filters are applied before search and again before context assembly.
Neighbor expansion is bounded by relationship allow-list, depth, candidate
count, and token budget.

Journey-specific planners:

- **Lore:** prefer reviewed direct evidence; include competing assertions and
  valid-time intervals; separate fact/theory/attributed belief.
- **Campaign:** require campaign ID and actuality; include planned content only
  when the query requests prep; enforce perspective/visibility before recall.
- **Notes:** bias toward recent accepted decisions, open questions, action-item
  state, and linked source notes; preserve personal/private visibility.

## 9. Storage contracts

### JSONL ledger

Authoritative semantic records live under `.loregraph/records`. Every record has
a deterministic ID. SQLite stores stage fingerprints, current-version pointers,
usage accounting, and job status; it is not the only semantic copy.

### Neo4j

Constraints enforce unique IDs. Indexes cover names/aliases/source text and
chunk embeddings. Loader-managed nodes/edges carry `loaderNamespace =
"loregraph"`. Projection cleanup targets only records in that namespace.

### Obsidian

Generated notes carry `generated_by: loregraph-loader`. Cleanup consults a
generated-file manifest and removes only stale marked files. Hand-authored
notes are never deleted. Generated entity notes are stable link targets.

## 10. Interfaces

Primary CLI:

```text
loregraph doctor
loregraph ingest sources.yaml
loregraph estimate [--sync]
loregraph extract
loregraph prepare-batch requests.jsonl
loregraph apply-batch results.jsonl
loregraph embed
loregraph audit
loregraph audit-sample review.csv
loregraph load-neo4j
loregraph export-obsidian
loregraph export-simple-schema schema.json --profile notes
```

The legacy `eberron-kg` alias may remain during migration, but all new examples
and automation use `loregraph`.

## 11. Cost controls

- no model provider by default;
- local deterministic processing first;
- per-source hosted-processing opt-in;
- changed-only fingerprints;
- eligible content-type allow-list;
- chunk, input-token, output-token, and estimated-dollar caps;
- batch mode when appropriate;
- local embeddings for owned/sensitive sources;
- extraction sampling and human precision audit before corpus expansion.

## 12. Security and privacy

Credentials come only from environment variables and are never serialized.
Private text remains local unless its exact source version explicitly permits
hosted processing. Visibility is inherited by chunks/assertions and may become
stricter, never looser, during derivation. Player-safe contexts are built from
authorized records only; prohibited text is not retrieved and redacted later.

## 13. Reliability and recovery

Each stage is fingerprinted and idempotent. Record files are replaced atomically.
A failed model or projection job leaves the last valid ledger intact. The audit
command checks referential integrity, schema closure, provenance, grounding,
current/superseded consistency, and vector metadata before projection.

## 14. Test strategy

Unit tests cover schema closure, routing, chunk boundaries, exclusions, strict
model schemas, quote grounding, deterministic mechanics, and identity rules.
Integration tests cover idempotent ingestion, rechunk replacement, audits,
valid Obsidian YAML, non-destructive cleanup, and Neo4j query generation.

Journey acceptance suites must include:

- lore conflict, alias, temporal, and citation cases;
- campaign planned-vs-occurred, cross-campaign isolation, rules precedence, and
  player-safe leakage cases;
- notes decisions, open questions, task dependencies, backlinks, and private
  visibility cases;
- an Eberron corpus run proving the generic defaults do not require the pack.

## 15. Deployment

The loader runs locally. Aura is an optional remote projection; Obsidian is an
optional local projection. The architecture app is a static HTML artifact and
does not contain source content or credentials. Configuration, schema, and
identity packs are versioned alongside code.

## 16. Key decisions

1. Use a reified assertion/evidence ledger, not direct edges as truth.
2. Keep source fragments addressable independently of retrieval chunks.
3. Use a generic kernel plus small closed profiles and optional packs.
4. Preserve plan/play, scope, visibility, and time as separate dimensions.
5. Use the local ledger as portability boundary; Aura and Obsidian are peers.
6. Prefer deterministic/local work and spend model tokens on ambiguity.
7. Treat Eberron as a validation pack, not a product namespace.

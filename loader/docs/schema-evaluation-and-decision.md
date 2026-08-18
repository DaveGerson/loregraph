# LoreGraph schema evaluation and implementation decision

**Decision date:** 2026-08-15  
**Inputs reviewed:** `dnd-campaign-kg-v1.0.0-draft.zip` and
`eberron-graphrag-spec.md`  
**Implementation:** `loregraph-loader` v0.2.0  
**Product boundary:** setting-, rules-system-, and domain-agnostic; Eberron is the initial validation pack

## Executive decision

Use a hybrid, but give the two inputs different jobs:

1. Treat the **D&D Campaign Knowledge Graph draft** as the long-term semantic
   contract. Its separations among assertion/evidence, source/version,
   plan/play, rules/version, character/build/state, and item definition/instance
   are the right foundations for both lore and actual campaign operations.
2. Treat the **focused Eberron spec** as the ingestion and initial validation-pack
   design. Its layout-aware parsing, identity seed, content
   classification, deterministic-first extraction, constrained profiles,
   validation, and retrieval stages are much easier to turn into a personal
   corpus builder.
3. Do **not** pass the comprehensive ontology to an LLM on every chunk. Use a
   small extraction profile selected by source/content, then validate and map
   its output into the richer assertion-based model.
4. Make a portable local evidence ledger—not Neo4j or Obsidian—the canonical
   ingestion output. Project that ledger into Aura and/or an Obsidian vault.
   This removes vendor lock-in and makes reprocessing safe.

The included implementation follows that decision. It is deliberately usable
without an API key for parsing, chunking, deterministic mentions/mechanics,
auditing, local embeddings, and Obsidian export. Hosted extraction is an
optional, capped enrichment stage.

## What was verified

The comprehensive draft is internally coherent. Its bundled validator passed
**12,618 static conformance checks** with no errors. The bundle contains:

- 619 taxonomy concepts across 39 schemes;
- 271 canonical predicates;
- 118 GraphQL node types; and
- 18 full-text indexes.

That is a strong application/backend model. It is not an efficient per-chunk
extraction schema: presenting hundreds of terms to a model repeatedly would
consume tokens, increase label confusion, and make entity resolution less
reliable.

The focused Eberron draft is a strong ingestion specification. It directly addresses
the hard parts of RPG books—two-column layouts, sidebars, tables, stat blocks,
contested canon, edition differences, aliases, and graph-aware retrieval. Its
main weakness is not lore; it is that its world/rules graph is too small to be
the only model for running campaigns over time.

## Schema comparison

| Need | Comprehensive DKG draft | Focused Eberron draft | Decision |
|---|---|---|---|
| General lore (Eberron validation) | Strong generic model, but no setting identity seed | Strong setting-specific types, relationships, seed plan, and contested-canon examples | Small lore profile mapped into generic assertion/evidence semantics |
| Conflicting claims and citations | Excellent `Assertion`/evidence/perspective model | Good `Claim` model, but ordinary facts rely heavily on relationship properties | Make `Assertion` authoritative for every statement |
| Published adventures vs. world truth | Explicit source/citation roles and actuality | Described in prose, less completely represented | `ADVENTURE_SOURCE + AUTHORED`; never infer campaign occurrence |
| Campaign prep vs. table history | Excellent planned definitions vs. immutable play-event/run records | Mostly out of scope | Keep DKG plan/play distinction |
| Character progression | Identity/build/state are separated | Mostly stat blocks and lore people | Keep `Character`, `CharacterBuild`, `CharacterState` |
| Inventory | Definition/instance/ownership model | Reusable item nodes, no full instance history | Keep `ItemDefinition` and `ItemInstance` |
| D&D rules evolution | Ruleset/version/rule-element-version hierarchy | Edition tags and mechanics layer, but limited version semantics | Pin SRD and campaign to immutable `RulesetVersion` records |
| Fictional time | Faithful instants/intervals and uncertainty | Convenient `from_yk`/`to_yk`, with a warning not to invent years | Preserve source date strings; normalize only when justified |
| Rights, attribution, access | Comprehensive | Personal-use intent and player-safe idea, but partial enforcement | Store rights/indexing/visibility/remote-processing policy per source |
| LLM extraction practicality | Too large unless profiled | Strong | Use seven closed profiles and deterministic preprocessing |
| Neo4j application API | Very strong | Good initial physical design | Keep DKG as future application contract |
| Obsidian usability | Not a primary target | Not a primary target | Generate YAML + wikilink projection from the same ledger |

## Required corrections and extensions

### 1. Make assertions authoritative

Relationship properties such as `source_chunks`, `source_quote`, and
`confidence` work for a prototype, but become fragile when two books disagree,
a claim is time-bounded, one fact has several sources, or access differs by
campaign. The canonical pattern should be:

```text
(Assertion)-[:SUBJECT]->(Entity)
(Assertion)-[:OBJECT]->(Entity)
(Assertion)-[:SUPPORTED_BY]->(SourceFragment)
(Assertion)-[:ASSERTED_BY]->(Entity)       # optional attribution
```

The assertion carries predicate, truth status, canon scope, actuality,
confidence, review state, visibility, and valid time. Familiar direct edges are
rebuildable Neo4j projections with `assertionId`. This keeps ordinary Cypher
pleasant without discarding epistemic/provenance semantics.

### 2. Separate four dimensions that are often conflated

- **Truth status:** asserted, disputed, rumored, believed, prophecy, false, or
  unknown.
- **Canon scope:** setting source, authored adventure, one campaign, homebrew,
  or none (rules).
- **Actuality:** authored description, planned content, occurred table history,
  or hypothetical branch.
- **Visibility:** public, player, DM-only, or private.

For example, “the villain is in room 12” in a published adventure is
`ADVENTURE_SOURCE + AUTHORED`; it is not `CAMPAIGN + OCCURRED`. A session plan is
`CAMPAIGN + PLANNED`. Only a session log or play event makes it
`CAMPAIGN + OCCURRED`.

### 3. Do not use unrestricted aliases for secrets

An alias list is fine for public lexical variants. It is unsafe for claims such
as a secret identity: a full-text alias index can leak that secret before an
access filter runs. Model the identity statement as a visibility-controlled
assertion (or a protected lexical entry), not as a flat alias.

### 4. Keep exact source versions

Use `Material -> MaterialVersion -> SourceFragment -> KnowledgeChunk`. File hash,
edition, distribution, rights, attribution, and parser metadata belong to the
version. Replacing a PDF creates a new immutable version; it does not silently
rewrite citations from the old one.

For the rules corpus, represent each SRD release as a distinct material and
ruleset version. Release-specific citations and future errata are exactly why
the graph should not use a floating “current SRD” identity.

### 5. Retain campaign-specific identity separations

The minimum durable campaign extensions are:

- `Character` vs. `CharacterBuild` vs. `CharacterState`;
- `CreatureArchetype` vs. `StatBlock` vs. named creature `Character`;
- `ItemDefinition` vs. `ItemInstance`;
- prepared `Scene`/`Encounter` vs. occurred `SceneRun`/`EncounterRun`;
- `RulesetVersion` and `RuleElementVersion`; and
- `WorldTimeInstant`/`WorldTimeInterval` with faithful text and uncertainty.

The shipped extraction profile therefore contains 27 high-value domain node
types and predicates divided among seven prompts. It is an ingestion profile,
not a claim that the comprehensive 118-type application schema is unnecessary.

### 6. Avoid label explosion

Keep stable, indexable labels such as `Faction`, `Location`, and `LoreEntity`.
Use a controlled subtype/concept for `dragonmarked-house`, `church`, `nation`,
`manifest-zone`, and similar leaf categories. This preserves pack-aware
questions without creating hundreds of physical labels or giving the model
nearly synonymous choices.

## Coverage of likely questions

| Question | Supporting model path |
|---|---|
| “Who led House Cannith at a given time?” | `Character <- SUBJECT - Assertion(LEADS, valid time) - OBJECT -> Faction`, with cited fragments |
| “What theories explain the Mourning?” | Competing assertions with truth status and `ASSERTED_BY`, never one flattened cause |
| “What does Thrane believe about the Blood of Vol?” | Belief/claim attributed to a faction, separate from world truth |
| “Which source says this, and on what page?” | Assertion → exact quote fragment → material version/page → material |
| “What did my party actually do?” | Campaign scope + occurred actuality + session/scene/encounter run |
| “What did I plan but never use?” | Planned scene/encounter with no realized run |
| “Which character had this item?” | Owner → `HAS_ITEM` → `ItemInstance` → `INSTANCE_OF_ITEM` → definition, time-bounded |
| “Which wording of this spell applies?” | `RuleElementVersion -> VERSION_OF -> RuleElement` and `FOR_RULESET_VERSION` |
| “Give a player-safe answer.” | Visibility filter is applied before graph expansion and context assembly |
| “What changed between two editions?” | Compare assertions/fragments attached to different immutable source and ruleset versions |

The model supports these questions. Answer quality still depends on extraction
recall and review; no schema can recover a fact that was never parsed or linked.

## Implemented loader architecture

| Stage | Behavior | LLM cost |
|---|---|---:|
| Manifest gate | Validates source kind, scope, actuality, indexing, visibility, attribution, page exclusions, and remote permission | None |
| Parse | Docling when installed; PyMuPDF fallback; Markdown/text support; OCR switch | None |
| Classify/chunk | Atomic tables/stat blocks/spells; heading breadcrumbs; 700 target/950 max tokens; zero overlap; previous/next links | None |
| Deterministic enrichment | Identity-only Eberron backbone, exact mentions, simple spell/stat-block fields | None |
| Portable ledger | Deterministic IDs, immutable versions, JSONL semantic records, SQLite stage fingerprints | None |
| Embed | Local sentence-transformers recommended; hosted option obeys per-source permission | Optional |
| Extract | Five closed Pydantic/JSON Schema profiles, exact-quote evidence, claims-only mode, changed-only fingerprints | Optional/capped |
| Resolve | Gazetteer ID, type-blocked exact alias, conservative fuzzy match, ambiguity queue | None after extraction |
| Validate | Type/predicate/profile, domain/range, grounding, referential integrity, provenance, vector dimensions | None |
| Project | Neo4j/Aura and/or Obsidian; stale managed output is safely converged | None |

Docling's current chunking guidance emphasizes hierarchy-aware chunks and a
token-aware `HybridChunker`, including headings/captions and table continuity
([Docling chunking documentation](https://docling-project.github.io/docling/concepts/chunking/)).
The loader follows the same structural principle but retains its own stable
chunk/evidence contract so either sink receives identical records. OCR and
custom conversion remain available for scanned books
([Docling conversion example](https://docling-project.github.io/docling/_generated/examples/custom_convert/),
[full-page OCR example](https://docling-project.github.io/docling/_generated/examples/full_page_ocr/)).

### Portable evidence ledger

The authoritative local files are:

```text
sources.jsonl     blocks.jsonl       chunks.jsonl
entities.jsonl    mentions.jsonl     evidence.jsonl
assertions.jsonl  review_queue.jsonl embeddings.jsonl
```

This is the key agnostic design choice. Neo4j-specific IDs, relationship
projections, and Obsidian file names are derived at export time. If a later
vector store, RDF store, or PostgreSQL implementation is preferable, no PDF or
LLM extraction has to be repeated.

### Neo4j/Aura projection

The loader creates source/version/chunk nodes, typed entities, lexical entries,
assertions, evidence links, mentions, full-text indexes, and a vector index when
embeddings are present. It marks superseded chunks and assertions, sweeps only
nodes previously marked as loader-managed, and rebuilds only loader-managed
direct projection edges.

Neo4j's supported Python GraphRAG package is currently `neo4j-graphrag`; its
documentation lists Neo4j/Aura support, vector/full-text components, multiple
LLM providers, local sentence-transformer embeddings, and entity resolvers
([package documentation](https://neo4j.com/docs/neo4j-graphrag-python/current/)).
Its knowledge-graph builder accepts a custom closed schema and offers exact,
fuzzy, and spaCy resolution, but the `SimpleKGPipeline` remains under the
experimental namespace
([KG builder guide](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_kg_builder.html)).

### Obsidian projection

Obsidian receives YAML properties and wikilinks for sources, entities,
assertions, and optional chunks. Obsidian properties are suited to scalar/list
metadata, while wikilinks drive backlinks and graph visualization
([properties](https://help.obsidian.md/properties),
[links](https://help.obsidian.md/links),
[graph view](https://help.obsidian.md/plugins/graph)). The exporter therefore
flattens recognized mechanic fields into properties and keeps complex evidence
as linked assertion notes. Obsidian is an excellent human review/notebook
projection, but not the authoritative store for bitemporal claims or access
control.

## Assessment of Neo4j Graph Builder

The public Neo4j Labs Graph Builder is useful for a one-book UI pilot. It
supports Aura, custom schemas, multiple providers, token tracking, and local
sentence-transformer embeddings
([Neo4j Labs repository](https://github.com/neo4j-labs/llm-graph-builder)). It is
not the recommended canonical ingest path for this corpus because:

- a generic document pipeline cannot preserve all of the proposed
  assertion/rights/actuality/version invariants by configuration alone;
- one large custom schema encourages expensive, confused extraction;
- its graph is the immediate output, making parser or ontology changes harder
  to replay independently; and
- direct relationships are insufficient for competing, attributed, differently
  visible claims.

Use it to answer: “Do these labels and questions feel useful on one book?” Do
not make that pilot database the only copy of the extraction.

## The obvious/easy implementation

The most useful “easy” answer is **not to extract the full semantic graph at
first**. A lexical-first hybrid graph already supports a large fraction of lore
questions:

```text
source/version -> heading-aware chunk -> exact entity mentions
                                   \-> local embedding + full-text index
```

For a question, retrieve chunks by vector/full-text similarity, expand one hop
through deterministic entity mentions, include adjacent chunks, and answer only
from those passages with page citations. This costs no extraction tokens, gives
immediate value, and produces the calibration data needed to decide which
relationships are worth extracting. The standard `ingest`, `embed`,
`load-neo4j`, and `export-obsidian` commands implement this durable easy mode.

For an even shorter but disposable semantic pilot, the included `easy-neo4j`
command wraps Neo4j's current
`SimpleKGPipeline` with the chosen closed profile:

```bash
python -m pip install -e '.[easy]'
loregraph --config config/project.toml easy-neo4j parsed-book.md --profile lore
```

Recommended easy-system protocol:

1. Parse one born-digital book locally into Markdown.
2. Use the `lore` profile only (9 node types, 34 predicates), not the whole
   campaign schema.
3. Ask 20 representative questions and inspect citations/entities.
4. Adjust the profile/backbone.
5. Treat the database as disposable and rebuild the production corpus from the
   evidence-ledger path.

This shortcut sends the supplied text to its configured hosted LLM and embedding
providers; unlike the manifest-driven pipeline, it cannot infer per-book remote
processing permission.

This is easier than custom extraction, but it does not eliminate the need for
source policy, assertion reification, and review if it becomes a durable system.

## Token and cost strategy

The loader's cost policy is intentionally conservative:

1. **Parse once and fingerprint everything.** LLM calls are changed-chunk only.
2. **Do deterministic work first.** The identity backbone, direct mentions,
   stat-block fields, and spell fields cost no model tokens.
3. **Use local embeddings for owned books.** Hosted embeddings remain opt-in per
   source. The example uses a long-context local model because Sentence
   Transformers truncates input beyond a model's `max_seq_length`; for example,
   its documentation lists only 256 tokens for `all-MiniLM-L6-v2`
   ([Sentence Transformers input-length guidance](https://www.sbert.net/examples/sentence_transformer/applications/computing-embeddings/README.html)).
   The configured GTE ModernBERT model card lists an 8,192-token input limit
   ([model card](https://huggingface.co/Alibaba-NLP/gte-modernbert-base)).
4. **Use zero-overlap extraction chunks.** Retrieval gets adjacent context via
   `NEXT_CHUNK`; repeated overlap would make the same facts paid input twice.
5. **Select one small profile per chunk/source.** Lore books never receive the
   character/campaign application taxonomy in their prompt.
6. **Require exact evidence quotes.** Bad output is quarantined before it can
   create graph facts.
7. **Set all four caps:** chunks, input tokens, maximum output tokens, and
   estimated dollars.
8. **Prefer asynchronous batch processing after calibration** when the configured
   provider offers a suitable lower-cost path.
9. **Use schema-constrained output.** The synchronous adapter uses Pydantic
   parsing; the Batch writer emits an all-fields-required, closed JSON Schema.
10. **Do not hard-code old pricing.** The example starts prices at zero and
    requires current inputs before the dollar cap is meaningful.

Do not estimate the full project from PDF file sizes. Ingest locally, then let
`loregraph estimate` count the actual selected prompt tokens after headers,
profiles, exclusions, and permission gates.

## Recommended rollout

### Milestone 0 — local corpus skeleton

- Ingest one born-digital sourcebook with no hosted processing.
- Inspect 50 chunks across two-column text, sidebars, tables, and stat blocks.
- Adjust excluded pages and parsing backend.
- Export to Obsidian and verify source/page navigation.

**Exit:** at least 95% of sampled chunks have usable reading order and correct
page range; no source lacks rights/visibility metadata.

### Milestone 1 — retrieval without semantic extraction

- Generate local embeddings.
- Load Aura or use Obsidian search.
- Test 20 real lore questions with vector/full-text retrieval and backbone
  mentions.

**Exit:** relevant source passages appear in the top results for at least 16/20
questions. Fix parsing/chunking before paying for relationship extraction.

### Milestone 2 — extraction calibration

- Opt in one source after deciding hosted-processing policy.
- Run 10–30 chunks synchronously.
- Audit every assertion and every entity merge.
- Tune predicates, quote rules, fuzzy threshold, and output cap.

**Exit:** at least 95% of accepted sampled assertions are supported by the cited
quote; ambiguous identities are queued rather than silently merged.

### Milestone 3 — one-book Batch and then corpus expansion

- Batch 100–250 high-value chunks from that book.
- Review a stratified sample by content type and confidence.
- Expand book by book; never process all 20 before checking the first one.

**Exit:** audits pass, cost per accepted assertion is acceptable, and repeated
runs add no duplicates.

### Milestone 4 — campaign and SRD profiles

- Add campaign notes as separate planned/occurred sources or snapshots.
- Use the `character` profile for sheets/build/state files.
- Pin SRD 5.2.1 as an immutable rules/material version with its exact legal
  attribution.
- Test player-safe retrieval separately from DM retrieval.

**Exit:** planned material never appears as occurred history; adventure text is
not promoted to campaign canon; rules answers name the applicable version.

## Acceptance checklist

- Every LLM/deterministic assertion has at least one source chunk and exact
  evidence fragment.
- Every chunk points to an immutable source version with page location.
- No unknown type, predicate, domain/range pair, or dangling endpoint passes the
  audit.
- Fiction is claims-only; adventures remain authored scenarios; campaign logs
  are explicitly occurred.
- Secret identities are not unrestricted aliases.
- Hosted processing occurs only for opted-in sources.
- Superseded chunks are not returned by default retrieval.
- Player retrieval filters visibility before graph expansion.
- Re-ingest with unchanged inputs is idempotent.
- Re-chunking one version removes its stale derived records without deleting
  other versions.
- Aura and Obsidian outputs can both be regenerated from the local JSONL ledger.

## Bottom line

The comprehensive DKG draft is the better **destination model**. The focused
ingestion draft is the better **way to get data into it**. The production-friendly answer
is the boundary between them: small generic extraction profiles plus optional packs feeding a
portable, assertion-and-evidence ledger, with Neo4j and Obsidian as projections.
That gives the personal project a cheap starting point without painting it into
a lore-only corner when campaign state, rules versions, secrets, and revisions
matter later.

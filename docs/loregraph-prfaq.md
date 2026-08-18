# LoreGraph PRFAQ

**Version:** 1.0  
**Status:** Working-backwards product narrative

## Press release

### LoreGraph turns personal libraries and campaign notes into cited, connected knowledge

**A local-first evidence graph answers lore questions, maintains tabletop
continuity, and connects everyday notes without locking the user into one
database or spending tokens on every page.**

Today we are introducing LoreGraph, a personal knowledge system that converts
owned books, PDFs, rules references, campaign records, and Markdown notes into
one evidence-backed knowledge ledger. Users can ask cross-source lore questions,
recall what actually happened at the table, or find decisions and follow-ups
across ordinary project notes. Every important answer can point back to the
specific source version, page, section, and supporting fragment.

Most retrieval systems force a trade-off between a flexible vector search box,
a carefully structured graph, and a human-editable notebook. LoreGraph treats
all three as views of the same portable ledger. Neo4j Aura provides rich graph
traversal and hybrid retrieval. Obsidian provides durable Markdown, backlinks,
and a workspace people can inspect or edit. Neither projection is the only copy
of truth.

LoreGraph also preserves distinctions that matter in real use. A published
adventure possibility is not a campaign event. A rumor is not an objective
fact. A rule from one release does not silently replace the version a campaign
uses. A private game-master secret or personal note is filtered out before
retrieval, rather than removed from an answer after the model has seen it.

The system minimizes model cost by doing structural parsing, chunking, hashing,
known-alias matching, validation, and most bookkeeping locally. Optional model
extraction uses a small profile for the current material—lore, rules, adventure,
campaign, character, or notes—and obeys explicit source permissions and hard
chunk, token, and cost caps.

Eberron is LoreGraph’s first validation corpus because it combines a large lore
library, edition-sensitive rules, published adventures, conflicting accounts,
and private campaign continuity. The product itself is setting-, system-, and
domain-agnostic; future identity and ontology packs can target other worlds or
knowledge domains without replacing the core.

## Customer FAQ

### What can I use LoreGraph for?

Three things are first-class: research and lore Q&A, campaign maintenance for a
game master, and ordinary knowledge/note-keeping. You can use only one journey
or combine them.

### Is it specific to Eberron or Dungeons & Dragons?

No. Eberron and a 5.5e-compatible SRD are the first test corpus. The core knows
about sources, entities, assertions, evidence, time, scope, visibility, and
review. Setting identities and rules systems are optional profiles or packs.

### Why not just put all the PDFs into a vector database?

Vector search is useful for recall but does not reliably preserve identity,
time, source conflicts, campaign continuity, rules versions, or access policy.
LoreGraph uses vectors alongside lexical search and a cited assertion graph.

### Why not just use Neo4j’s graph builder?

It is a good pilot path for discovering useful labels and questions. The durable
system adds source/version lineage, rights gates, plan-vs-play semantics,
campaign scope, a review queue, an Obsidian projection, and a portable ledger.

### Can I use Obsidian without Neo4j?

Yes. The vault is a complete human-readable projection with source and entity
notes, YAML properties, and backlinks. Neo4j becomes valuable for more complex
traversal and retrieval, but it is optional.

### Will export overwrite my notes?

No. Generated files are placed in marked namespaces and carry a generator
marker. Cleanup removes only stale files that LoreGraph previously generated;
unmarked hand-authored Markdown is left alone.

### Does LoreGraph send all of my books to an LLM?

No. Model use is off by default. Each source version must explicitly permit
hosted processing, and configured chunk/token/cost caps are checked before any
call. Local embeddings and deterministic processing are preferred for private
material.

### How does it handle contradictory lore?

It stores claims as separate evidence-backed assertions with attribution,
scope, valid time, confidence, and review state. Retrieval can present competing
accounts instead of flattening them into one fact.

### How does it know what actually happened in my campaign?

Campaign records distinguish prepared definitions (`Scene`, `Encounter`) from
occurred runs and session evidence. A history query filters for the selected
campaign and `OCCURRED` actuality; skipped prep stays out unless requested.

### Can players use it without seeing spoilers?

The design supports player-safe retrieval. Principal, campaign membership,
visibility, perspective, and spoiler tier are applied before search and graph
expansion. This must be covered by leakage tests before player release.

### What does the notes journey add?

A compact profile connects people, organizations, topics, projects, action
items, decisions, questions, events, dates, and places. It can answer “what was
decided?” or “what remains open?” with backlinks and citations.

### How much manual review is required?

Deterministic parsing and exact identity matches require little review. Model-
derived assertions begin unreviewed, and ambiguous identity matches enter a
queue. A small calibration sample is expected before each new source class or
large corpus expansion.

### Can I move away from Aura or Obsidian later?

Yes. The JSONL evidence ledger is the portability boundary. Both existing
projections can be rebuilt, and another sink can consume the same records.

## Internal FAQ

### What is the smallest viable first release?

Local PDF/Markdown ingestion, deterministic chunks, rights metadata, the
generic lore/campaign/notes profiles, conservative entity resolution, evidence
and assertion records, audit, and an Obsidian export. Neo4j and optional model
extraction can be activated after local records pass review.

### Why is Assertion a node instead of just a relationship?

Because evidence, attribution, truth status, scope, valid time, visibility,
confidence, review, contradiction, and supersession belong to a statement.
Direct relationships are still materialized for fast traversal and carry the
assertion ID that produced them.

### Why are SourceFragment and KnowledgeChunk separate?

Fragments are durable citation addresses; chunks are changing retrieval units.
The system can improve chunking or embeddings without invalidating citations.

### Why use several small profiles instead of one large ontology prompt?

Smaller profiles reduce prompt tokens, improve schema compliance, simplify
validation, and make domain extensions governable. The kernel reconciles their
output after extraction.

### What is the biggest technical risk?

False confidence from incorrect entity merges or attractive but unsupported
assertions. Conservative thresholds, exact quote grounding, review queues,
scope isolation, and sampled evaluation mitigate the risk.

### What is the biggest product risk?

Building a graph that is elaborate but not useful in daily workflows. The three
journeys therefore have explicit acceptance questions, and the roadmap starts
with a small end-to-end corpus before ontology expansion.

### How will we know the design is generic?

The default configuration uses an empty identity pack; the notes journey must
work without any TTRPG types; a non-Eberron lore source must ingest under the
generic lore profile; and Eberron-specific behavior must require an explicit
pack/profile choice.

### What is the “obvious easy” implementation?

For a disposable pilot, use Neo4j’s simple graph pipeline on pre-parsed text
with one closed profile. For the lowest-operations durable start, run the local
ledger plus Obsidian and postpone Aura. Both paths validate value while keeping
the evidence-ledger architecture available for expansion.

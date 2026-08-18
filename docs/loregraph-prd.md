# LoreGraph Product Requirements Document

**Version:** 1.0  
**Status:** Build-ready draft  
**Initial validation corpus:** Eberron and the D&D 5.5e-compatible SRD  
**Product boundary:** Setting-, system-, and domain-agnostic

## 1. Product summary

LoreGraph turns owned reference material and private notes into a trustworthy,
cited knowledge system. It serves three first-class journeys from one portable
evidence ledger:

1. general lore and reference support;
2. tabletop campaign maintenance for a game master; and
3. general knowledge and note-keeping.

Neo4j Aura and Obsidian are interchangeable projections, not competing sources
of truth. Eberron is the first demanding test because it combines many books,
conflicting or edition-sensitive lore, rules, adventures, private table state,
and spoiler boundaries.

## 2. Problem

Large personal libraries are hard to search reliably. Keyword search misses
aliases and relationships; vector search can retrieve plausible but wrong
passages; plain wikis flatten disagreement and time; campaign notes mix plans
with events that actually occurred; and generic graph builders often lose
source, edition, rights, or review status.

The product must answer useful questions without pretending every extracted
statement is equally true, current, authorized, or part of the same continuity.

## 3. Product principles

- **Evidence before convenience.** Every meaningful assertion can cite an
  addressable source fragment and material version.
- **One kernel, small extensions.** A generic identity/assertion/source model is
  extended by lore, campaign, rules, and notes profiles.
- **Plans are not history.** Authored, planned, occurred, hypothetical, and
  superseded records remain distinguishable.
- **Access before retrieval.** Visibility and rights filters run before vector
  search or graph expansion.
- **Local first, LLM selective.** Parsing, chunking, hashes, known aliases,
  validation, and most matching run locally. Models handle only ambiguous or
  high-value semantic work within explicit budgets.
- **Portable by design.** The local evidence ledger is authoritative; graph,
  vector, vault, and application views are rebuildable.

## 4. Users and jobs

### Lore researcher

Needs to ask cross-source questions, explore people/places/events/factions,
distinguish established claims from theories, and inspect exact citations.

### Game master

Needs to prepare sessions, protect secrets, track characters and state, record
what actually happened, pin the applicable rules version, and recall campaign
continuity months later.

### Knowledge worker or personal note-taker

Needs to capture documents and notes, connect people/topics/projects, recover
decisions and open questions, and continue working in an editable Obsidian
vault without surrendering provenance.

## 5. Key journeys

### Journey A — General lore support

**Entry:** Add books, articles, maps, setting notes, or a curated identity pack.  
**System behavior:** Register rights and editions, parse/chunk, route to the lore
profile, resolve aliases, preserve conflicting claims, and index graph plus
vectors.  
**Outcome:** Ask a question and receive a scoped answer that separates facts,
attributed beliefs, disputed claims, uncertainty, and source editions.

Acceptance examples:

- “Who led this organization during the requested era?” respects valid time.
- “What caused this catastrophe?” presents competing theories without choosing
  one unless the selected continuity establishes it.
- Every substantive answer paragraph exposes one or more source citations.

### Journey B — DM campaign maintenance

**Entry:** Add adventure material, campaign notes, character snapshots, rules,
and hand-authored prep.  
**System behavior:** Separate reusable definitions from instances and plans from
runs; scope every record to a campaign; version builds/state; maintain session,
scene, encounter, item, clue, secret, and consequence links.  
**Outcome:** Recall history, prepare future play, answer rules questions against
the pinned ruleset, and provide spoiler-safe player views.

Acceptance examples:

- “What did the party actually do?” excludes prepared but skipped encounters.
- “What does this character know?” filters DM-only truth before graph expansion.
- A house rule or ruling can override a base rule with explicit precedence and
  provenance.

### Journey C — Knowledge and note-keeping

**Entry:** Add Markdown notes, PDFs, documents, meeting records, or webpages.  
**System behavior:** Route to a compact notes profile; extract people,
organizations, topics, projects, action items, decisions, questions, events,
time, and location; render editable notes and backlinks.  
**Outcome:** Find what was decided, discover related notes, review open
questions/follow-ups, and keep the Obsidian vault useful without Neo4j.

Acceptance examples:

- “What decisions were made about Project Northstar?” returns cited decisions.
- “Which action items depend on this milestone?” traverses explicit dependencies.
- Re-export never deletes or overwrites an unmarked hand-authored Markdown file.

## 6. Functional requirements

### FR-1 Source registry and rights

The system shall register a conceptual material and immutable material version,
including checksum, edition, source kind, scope, visibility, attribution,
indexing mode, and hosted-processing permission.

### FR-2 Addressable parsing and chunking

The system shall preserve page ranges, heading paths, selectors, exact-text
hashes, reading order, tables/stat blocks as atomic units, and neighbor links.
Chunking shall be deterministic and idempotent for a given source/config hash.

### FR-3 Profile routing

The system shall choose the smallest closed extraction profile applicable to a
source or chunk. The generic kernel and profiles shall not require Eberron or a
specific role-playing rules system.

### FR-4 Entity resolution

The system shall resolve deterministic IDs and exact normalized aliases first,
then apply type, scope, campaign, ruleset, time, and context constraints. It
shall queue ambiguous matches rather than force-merge them.

### FR-5 Assertion ledger

The system shall store subject, predicate, object/literal, evidence, truth
status, canon/knowledge scope, actuality, visibility, valid time, record time,
confidence, extraction method, review state, and optional attribution.

### FR-6 Campaign model

The system shall distinguish definition from instance, planned scene/encounter
from occurred run, character identity from build/state, and source canon from
campaign canon. All campaign records shall be continuity-scoped.

### FR-7 Notes model

The system shall provide a compact profile for Person, Organization, Topic,
Project, ActionItem, Decision, Question, Event, Time, and Location with common
work relationships.

### FR-8 Projections

The system shall export the ledger to Neo4j Aura and Obsidian independently.
Projection failures shall not corrupt the ledger, and a projection shall be
safe to rebuild.

### FR-9 Retrieval

The system shall apply hard policy/scope filters, combine lexical and vector
recall, expand a bounded entity/assertion neighborhood, rerank for evidence
quality, and assemble a cited context pack.

### FR-10 Review and cost controls

The system shall estimate eligible chunks/tokens/cost before model calls, cap
usage, process only changed content, support calibration samples, and expose a
review queue for ambiguous identities and unreviewed assertions.

## 7. Non-functional requirements

- Local-only ingestion must work with no API key.
- Re-running unchanged inputs must produce no semantic duplicates.
- A source version must remain citable after rechunking or supersession.
- Secrets and private notes must be deny-by-default.
- The core ledger must be readable without Neo4j.
- The system should support a 20–30 book personal corpus on commodity hardware;
  model usage must be bounded by configuration.
- All generated IDs, profiles, and predicate definitions must be deterministic
  and schema-valid.

## 8. MVP scope

In scope:

- PDF/Markdown ingestion, page-aware chunking, hashes, rights metadata;
- seven closed extraction profiles including notes and an Eberron validation
  profile;
- exact/backbone resolution plus review queue;
- assertion/evidence JSONL ledger and audit;
- Neo4j and Obsidian projections;
- local or configured hosted embeddings;
- synchronous or batch model extraction with hard budgets;
- retrieval recipes and architecture/product documentation.

Out of scope for MVP:

- multiplayer sync and hosted account management;
- automatic copyright or license determinations;
- autonomous canon decisions or silent conflict resolution;
- replacing a full virtual tabletop;
- unrestricted web crawling;
- automatic publication of player-visible material.

## 9. Success measures

- ≥95% of sampled answer claims have a valid fragment citation.
- 0 known cross-campaign entity merges in the pilot corpus.
- 0 DM-only fragments appear in player-safe retrieval tests.
- ≥90% of deterministic alias matches are correct in a human audit sample.
- Unchanged re-ingestion creates 0 duplicate chunks/entities/assertions.
- The median common query can be answered from a context pack small enough for
  one answer call, without sending entire books.
- Obsidian-only users can browse source/entity links and recover a cited answer
  without Neo4j being available.

## 10. Delivery sequence

1. Validate local parsing, chunks, rights, and Obsidian output on three sources.
2. Calibrate the generic lore profile and Eberron pack on 10–30 chunks.
3. Load one setting book and one SRD version into Neo4j; evaluate retrieval.
4. Add one campaign and verify planned/occurred and player-safe tests.
5. Add ordinary project/meeting notes and verify the notes journey.
6. Expand the corpus incrementally, accepting only reviewed extraction changes.

## 11. Risks and mitigations

- **Extraction cost:** changed-only processing, local embeddings, compact
  profiles, token/chunk/dollar caps, batch mode.
- **False identity merges:** scope/type gates, conservative thresholds, review
  queue, reversible merge records.
- **Source conflicts:** assertion reification, attribution, valid time, source
  authority policy, no destructive flattening.
- **Spoiler leakage:** deny-first access filter before every retrieval stage.
- **Schema sprawl:** generic kernel plus profile/pack governance; leaf concepts
  use subtypes rather than new labels unless query value justifies them.
- **Vault drift:** generated namespaces/markers and non-destructive export.

## 12. Release acceptance

The MVP is releasable when all three journey suites pass, schema/audit tests are
green, the same ledger can rebuild both projections, the app documentation
matches the checked-in schema, and an Eberron pilot works without any Eberron
assumption in the generic default configuration.

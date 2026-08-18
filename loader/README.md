# LoreGraph Loader

LoreGraph is a provenance-first, storage-agnostic loader for three related uses:

1. answering general lore and reference questions with citations;
2. maintaining a tabletop campaign as plans become played history; and
3. connecting ordinary notes, projects, decisions, questions, and follow-ups.

Eberron is the first validation corpus, not a hard-coded product boundary. The
default configuration is setting-neutral; `config/backbone.eberron.json` and
the `lore_eberron` extraction profile are optional test-pack assets.

The loader accepts PDF and Markdown sources, preserves page/section lineage,
creates deterministic chunks, optionally calls an LLM through small closed
profiles, resolves safe identity matches, audits all assertions, and projects
the same evidence ledger into Neo4j Aura and/or an Obsidian vault.

## Architectural contract

- `Material -> MaterialVersion -> SourceFragment -> KnowledgeChunk` preserves
  source, edition, selector, and page provenance.
- `Assertion` is the authoritative claim object. Truth status, scope,
  plan-vs-play actuality, visibility, confidence, review state, attribution,
  and valid time remain independent.
- Neo4j domain edges and Obsidian notes are rebuildable projections. Every
  materialized relationship carries its `assertionId`.
- Ambiguous identity candidates create review items; they are never silently
  force-merged.
- Hosted processing is denied per source unless
  `remote_processing_allowed: true` is explicit.
- The smallest applicable profile is used for each source: `lore`,
  `lore_eberron`, `rules`, `adventure`, `campaign`, `character`, or `notes`.

## Journey coverage

### Lore support

Use `source_kind: setting` and profile `lore`. The loader preserves conflicting
sources, aliases, historical time, attribution, and exact citations. A setting
pack may add identity seeds or specialized predicates without changing the
kernel. Eberron inputs may select `lore_eberron` and the bundled Eberron
backbone.

### DM campaign maintenance

Use `adventure`, `campaign`, and `character` profiles. Prepared `Scene` and
`Encounter` records remain separate from occurred `SceneRun`, `EncounterRun`,
and play-event evidence. Character identity is separate from build and runtime
state. Campaign, visibility, ruleset version, and session filters prevent
published possibilities or DM secrets from leaking into player-safe answers.

### Knowledge and note-keeping

Use `source_kind: note`, `document`, or `web`; the loader selects the compact
`notes` profile. It recognizes `Person`, `Organization`, `Topic`, `Project`,
`ActionItem`, `Decision`, `Question`, `Event`, time, and location relationships.
The generated Obsidian vault provides YAML properties, backlinks, source notes,
and optional evidence/assertion notes while leaving hand-authored files alone.

## Install

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[docling,openai,neo4j,local-embeddings]'
```

For a local-only trial, `python -m pip install -e .` is enough. Docling is
recommended for multi-column pages, tables, sidebars, and OCR; PyMuPDF is the
lightweight fallback.

## Configure

```bash
cp config/project.example.toml config/project.toml
cp config/sources.example.yaml sources.yaml
```

Each source declares its kind, scope, actuality, rights/indexing mode,
visibility, hosted-processing permission, and optional ruleset/campaign ID.
Use `excludes: [[start, end]]` for indexes, advertisements, and legal pages.
The manifest supports the top-level key `sources`; legacy `books` manifests
remain readable.

Keep credentials outside project files:

```bash
export OPENAI_API_KEY='...'
export NEO4J_URI='neo4j+s://YOUR_INSTANCE.databases.neo4j.io'
export NEO4J_USERNAME='neo4j'
export NEO4J_PASSWORD='...'
export NEO4J_DATABASE='neo4j'
```

The application reads a credential only when the corresponding command runs;
it never writes keys into the ledger or vault.

## Local-first workflow

```bash
loregraph --config config/project.toml doctor
loregraph --config config/project.toml ingest sources.yaml
loregraph --config config/project.toml audit
loregraph --config config/project.toml export-obsidian
```

This path makes no LLM calls with the example configuration. It creates
layout-aware chunks, zero-overlap neighbor links, optional backbone mentions,
deterministic mechanics structures, rights/source records, and the Obsidian
projection. Re-ingestion is idempotent. A changed file becomes a new immutable
`MaterialVersion`; a parser or chunking change replaces only derived records
for that version.

## Budgeted enrichment

Local embeddings are recommended for owned or sensitive material. Set
`embeddings.provider = "sentence_transformers"` and run:

```bash
loregraph --config config/project.toml embed
```

LLM extraction is optional. Update provider prices, inspect the estimate, and
start with a 10-30 chunk calibration:

```bash
loregraph --config config/project.toml estimate
loregraph --config config/project.toml extract
loregraph --config config/project.toml audit
loregraph --config config/project.toml audit-sample review.csv --size 25
```

The gates are cumulative: source permission, allowed content type, changed-only
fingerprint, chunk cap, token cap, and dollar cap. For asynchronous processing,
use `prepare-batch` and `apply-batch`. Downloaded output is validated locally;
unknown types, invalid domain/range pairs, unsupported quotations, and dangling
endpoints are quarantined.

## Projections

After a clean audit:

```bash
loregraph --config config/project.toml load-neo4j
loregraph --config config/project.toml export-obsidian
```

Neo4j receives source, chunk, entity, mention, assertion, evidence, embedding,
and optional direct traversal edges. Obsidian receives generated source/entity
notes, optional assertion/evidence notes, YAML properties, and wikilinks.
Generated notes are marked `generated_by: loregraph-loader`; cleanup never
deletes an unmarked Markdown file.

The local `.loregraph/records/*.jsonl` evidence ledger remains authoritative.
SQLite stores stage fingerprints and usage accounting only.

## Easy pilot path

Neo4j's experimental `SimpleKGPipeline` can validate labels and questions on a
small, deliberately approved corpus:

```bash
python -m pip install -e '.[easy]'
loregraph --config config/project.toml easy-neo4j sample.md --profile lore
```

This path does not implement the canonical assertion ledger, source-rights,
campaign actuality, or revision semantics. It is a disposable discovery path,
not the durable corpus architecture.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests cover schema closure, generic notes routing, domain/range validation,
strict Batch output, quote grounding, page exclusion, atomic mechanics,
entity resolution, rechunk cleanup, idempotence, audit integrity, and Obsidian
frontmatter.

## Boundaries

- No sourcebook or SRD text is distributed with this project.
- Rights and remote-processing flags are technical safeguards, not legal
  advice.
- Setting backbones contain identity/alias seeds only; they are not lore facts.
- All model-extracted assertions begin `UNREVIEWED`.
- Player-facing retrieval must filter visibility before vector search or graph
  expansion.
- The `eberron-kg` console alias is retained temporarily for compatibility;
  new automation should use `loregraph`.

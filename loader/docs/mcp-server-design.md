# LoreGraph MCP Server — Design

**Status:** Implemented (`loregraph-mcp`, package `loregraph.mcp_server`)
**Audience:** LoreGraph maintainers and anyone wiring an agent to a LoreGraph graph

## 1. Purpose

LoreGraph projects its evidence ledger into Neo4j so that questions can be
answered with graph traversal and hybrid search. The MCP server is the
retrieval/context service from TDD §3: a local process that lets any MCP-capable
agent (Claude Code, Claude Desktop, IDE agents, custom SDK agents) pull cited
context from that projection wherever it lives: a local Neo4j, Neo4j Aura, or
any other Bolt endpoint.

It exposes LoreGraph concepts (passages, entities, assertions, evidence,
sources) instead of raw Cypher. That makes agents productive without schema
knowledge, and it lets the server enforce the product's deny-first access rules
(PRD §3 "Access before retrieval") on every call.

### Goals

1. One command, three deployment targets: local, Aura, and self-hosted or
   cloud Neo4j, including several at once.
2. Deny-first access policy that is fixed by whoever launches the server and
   cannot be widened by the agent.
3. Every answer is citable: source title, edition, pages, heading, exact quote,
   and required attribution text.
4. Truth, review, actuality, scope, and campaign dimensions are preserved in
   results, never flattened.
5. Strictly read-only, bounded in time and size, and never leaks credentials.
6. Works with zero LoreGraph config (just `NEO4J_*` variables) and improves
   when the project TOML is supplied.

### Non-goals

- Writing to the graph. The JSONL ledger is the write model (TDD §3); the
  server never mutates the projection.
- Answer generation. The agent composes answers; the server supplies context.
- Multi-user authentication for the HTTP transport. Run one server per trust
  boundary, or put it behind your own authenticating proxy.

## 2. Architecture

```text
agent ──MCP (stdio | streamable HTTP)──> loregraph-mcp
                                          │
          server.py   typed tool surface, instructions, error mapping
          retrieval.py LoreGraphReader: policy-filtered Cypher, ranking, shaping
          cypher.py    pure helpers: filter fragments, Lucene escaping, RRF,
                       citations, raw-Cypher guard (unit-tested offline)
          embedder.py  optional query embedder (must match the indexed model)
          graph.py     GraphGateway: one driver per connection, READ transactions,
                       timeouts, row caps, JSON conversion, error translation
          settings.py  connection profiles, access policy, config loading
                                          │ Bolt (neo4j://, neo4j+s://, bolt+s://, …)
                        ┌─────────────────┼──────────────────┐
                  local Neo4j        Neo4j Aura       self-hosted / cloud
```

Tools are synchronous functions; the MCP SDK runs them on worker threads. The
Neo4j driver is thread-safe and drivers are created lazily per connection, so
an unreachable secondary connection never blocks startup.

## 3. Connections: local, Aura, or elsewhere

A connection profile names one deployment. Values ending in `_env` name
environment variables, so URIs, users, and secrets can all stay out of files.

| Target | URI | Notes |
| --- | --- | --- |
| Local Neo4j / Desktop / Docker | `neo4j://localhost:7687` or `bolt://localhost:7687` | Plain Bolt is fine on loopback. |
| Neo4j Aura | `neo4j+s://<id>.databases.neo4j.io` | Encryption is required; the server rejects non-`+s` Aura URIs. |
| Self-hosted with public CA | `neo4j+s://graph.example.com:7687` | Full certificate verification. |
| Self-hosted with private CA | `neo4j://graph.internal:7687` + `trusted_ca = "ca.pem"` | Pins your CA; `+s` schemes cannot be combined with a custom CA. |
| Self-signed, testing only | `neo4j+ssc://…` | Accepted, but `status` warns that any certificate is trusted. |
| SSO / OIDC token | `auth = "bearer"`, `token_env = "…"` | For deployments that issue bearer tokens. |

With no `[mcp.connections]` table, the server builds a `default` connection
from `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, and `NEO4J_DATABASE`, the
same variables `loregraph load-neo4j` already uses. Profiles are validated at
startup: unknown schemes, credentials embedded in the URI, inline `password`
keys, and missing CA files are configuration errors. The `status` tool reports
each connection's kind (`local`, `aura`, `remote`), encryption, and warnings
(unencrypted remote host, `+ssc` trust-all, unset secret variable), but never
the secret itself.

`--env-file` loads `KEY=VALUE` files so MCP client configs need not embed
passwords. Aura's downloadable credentials file uses this format, so it can
be passed directly.

```toml
[mcp]
default_connection = "aura"
max_rows = 200
query_timeout_seconds = 20
query_embeddings = "auto"      # auto | none | sentence_transformers | openai
allow_raw_cypher = false

[mcp.policy]
audience = "dm"                # player | dm | owner
# visibilities = ["PUBLIC", "PLAYER"]      # optional, may only narrow the audience
# review_statuses = ["ACCEPTED"]           # default: ACCEPTED, UNREVIEWED, NEEDS_REVIEW
# canon_scopes = ["SETTING_SOURCE", "CAMPAIGN"]
# campaign_ids = ["campaign:alpha"]

[mcp.connections.local]
uri = "neo4j://localhost:7687"
password_env = "NEO4J_LOCAL_PASSWORD"

[mcp.connections.aura]
uri_env = "NEO4J_URI"
username_env = "NEO4J_USERNAME"
password_env = "NEO4J_PASSWORD"
database_env = "NEO4J_DATABASE"

[mcp.connections.homelab]
uri = "neo4j://graph.internal:7687"
trusted_ca = "certs/homelab-ca.pem"
password_env = "HOMELAB_NEO4J_PASSWORD"
```

For the audience, default connection, and config path, a CLI flag wins over
the environment (`LOREGRAPH_MCP_AUDIENCE`, `LOREGRAPH_MCP_CONNECTION`,
`LOREGRAPH_CONFIG`), which wins over the project file. Every
tool accepts an optional `connection` argument, so one server can compare a
local working copy with the Aura projection.

## 4. Access policy

The policy is chosen by the operator at launch and applied inside every Cypher
statement, before results leave the database, and again when passages are
hydrated. Tool arguments can narrow it (one campaign, fewer scopes, accepted
assertions only) but any request outside it fails.

| Audience | Visibilities | Intended use |
| --- | --- | --- |
| `player` (default) | PUBLIC, PLAYER | Player-facing agents; safe default. |
| `dm` | + DM_ONLY | Game-master prep and continuity. |
| `owner` | + PRIVATE | The corpus owner's personal agent; the only audience that may enable raw Cypher. |

Rules, per record type:

- **Chunks:** visibility allowed, indexing mode permits text (never `NO_INDEX`
  or `METADATA_ONLY`), `ACTIVE` unless superseded material is requested,
  canon scope and campaign allowed.
- **Assertions:** the same dimensions plus review status; `REJECTED` is
  excluded by default; `active = true` unless superseded material is requested.
- **Entities:** projection nodes carry no visibility. An entity is visible only
  if an allowed chunk mentions it or an allowed assertion references it, and
  its campaign is allowed. Identity seeds that no permitted evidence touches stay
  hidden. Entity `description` and `structuredData` are withheld unless the
  policy is unrestricted, because they have no visibility label of their own.
- **Campaigns:** records without a campaign ID (setting lore, rules, notes)
  pass the campaign filter; campaign-scoped records must match. Use
  `canon_scopes=["CAMPAIGN"]` plus `campaign_id` for strictly one campaign.
- **Missing vs hidden:** lookups of hidden ids return the same "not found or
  not visible" message as missing ids. Name resolution never substitutes a
  different entity when the requested one is hidden; it reports the nearest
  visible names instead.

`graph_schema` counts only visible records, so an agent cannot learn how much
material is withheld.

## 5. Tool catalog

All tools are annotated read-only and idempotent.

| Tool | Purpose | Key arguments |
| --- | --- | --- |
| `status` | Connection, Neo4j version, policy, index states, vector readiness. First stop when anything fails. | `connection` |
| `graph_schema` | Visible entity types, predicates with domain/range, campaign ids, counts, and the LoreGraph data dictionary. | — |
| `search_passages` | Full-text, vector, or hybrid passage search with citations, mentions, neighbor ids. | `query`, `mode`, `campaign_id`, `canon_scopes`, `source_codes`, `content_types`, `include_superseded`, `max_chars` |
| `get_passage` | One chunk plus up to 3 neighbors per side in reading order. Superseded chunks stay readable by id so old citations resolve. | `chunk_id`, `neighbors` |
| `search_entities` | Typo-tolerant name/alias lookup. | `query`, `entity_types`, `campaign_id` |
| `get_entity` | Identity, assertion counts by predicate, strongest assertions with evidence, mentioning passages with snippets. | `entity` (id or name) |
| `find_assertions` | The authoritative claims with every dimension and quoted evidence. | `entity`, `predicates`, `direction`, `other_entity`, `actualities`, `truth_statuses`, `review_statuses` |
| `find_connections` | Shortest chains of permitted assertions between two entities (≤ 4 hops). | `source`, `target`, `max_hops` |
| `list_sources` | Visible source versions with edition, attribution, and chunk counts. | `source_kinds`, `campaign_id` |
| `read_cypher` | Registered only when `allow_raw_cypher` is on and the policy is unrestricted. | `query`, `parameters`, `limit` |

Entity arguments accept an id or a name. The response's `resolution` block
records `id`, `exact_name`, or `close_match` plus alternatives, so the agent can
correct a wrong guess.

The server's MCP `instructions` tell agents the intended workflow (search,
then inspect, then cite) and the interpretation rules: unreviewed means
unverified, disputes are presented with sources, plans are not history,
campaigns are never merged, and an empty result does not prove absence.

## 6. Retrieval mechanics

- **Full-text:** agent text is tokenized and every Lucene metacharacter escaped,
  so arbitrary input never raises a parser error. The exact phrase is boosted
  and terms are OR-ed. Entity search adds `~1` fuzziness for terms of five or
  more characters.
- **Vector:** available only when the vector index is `ONLINE`, a query
  embedder is configured, and its model matches the `embeddingModel` recorded on
  indexed chunks. Vectors from different models are not comparable, so a
  mismatch disables vector mode with a reason. Hosted query embedding (OpenAI) is
  never inferred from `[embeddings]`; it must be selected with
  `mcp.query_embeddings = "openai"`, because query text can be as sensitive as
  the corpus. Index procedures filter after recall, so the server over-fetches
  (6× the limit, at least 50) before applying the policy.
- **Hybrid:** full-text and vector rankings are fused with reciprocal rank
  fusion (k = 60). `auto` uses hybrid when vectors are ready, else full-text. If
  the embedder fails at query time, hybrid degrades to full-text with a note.
- **Hydration:** ranked ids are re-read with the chunk filter applied again,
  then joined to source versions for citations. Text is truncated at a word
  boundary to the requested budget and marked `truncated`.
- **Citations:** `Title (edition), p. N — Heading`, with ` [superseded version]`
  for non-current versions and the attribution text appended for
  `FULL_TEXT_WITH_ATTRIBUTION` sources.
- **Compatibility:** queries avoid `CALL () {}` and `SEARCH`, so they run on
  Neo4j 5.18+ and Aura, which matches the loader's documented floor.

## 7. Safety controls

| Risk | Control |
| --- | --- |
| Writes through any tool | Every statement runs in a `READ` transaction; Neo4j rejects writes with `Statement.AccessMode`. |
| Raw Cypher misuse | Off by default. Allowed only for the unrestricted owner policy. `EXPLAIN` must report query type `r`. Textual guard rejects multiple statements, `LOAD CSV`, `USE`, `dbms.*`/`gds.*`, APOC namespaces that reach files/URLs or run nested Cypher, and procedures outside a read-only allow-list. |
| Runaway queries | Server-side transaction timeout (default 20 s) on every statement. |
| Context flooding | Per-tool limits, a global `max_rows` cap, text budgets, and dropped vector properties. |
| Credential leakage | Secrets only from environment variables; inline `password`/`token` keys and URI user-info are rejected; `status` and errors print scheme, host, and port only. |
| Network exposure | stdio by default. HTTP binds 127.0.0.1 with DNS-rebinding protection, and non-loopback binds are refused unless `--allow-remote-clients` is passed. |
| Planner noise | Server notifications are disabled on the driver so deprecation warnings don't reach logs or agents. |

Defense in depth for shared or remote databases: connect with a Neo4j user
that has only read privileges (Enterprise/Aura RBAC). The server's read
transactions already block writes, but a reader role also protects against
future bugs.

## 8. Running it

```bash
python -m pip install -e '.[mcp]'                 # add local-embeddings for vector search
loregraph-mcp --config config/project.toml --audience dm --check   # verify, then exit
```

Claude Code:

```bash
claude mcp add loregraph -- /abs/path/.venv/bin/loregraph-mcp \
  --config /abs/path/config/project.toml --env-file /abs/path/.env --audience dm
```

Claude Desktop or any client that reads an `mcpServers` map:

```json
{
  "mcpServers": {
    "loregraph": {
      "command": "/abs/path/.venv/bin/loregraph-mcp",
      "args": ["--config", "/abs/path/config/project.toml",
               "--env-file", "/abs/path/.env", "--audience", "player"]
    }
  }
}
```

HTTP clients: `loregraph-mcp --transport streamable-http --port 8765` serves
`http://127.0.0.1:8765/mcp`.

## 9. Testing

- `tests/test_mcp_server.py` runs offline with no extras: settings precedence,
  profile validation, secret redaction, env files, audience nesting, policy
  narrowing, Lucene escaping, name similarity, RRF, citations, and the
  raw-Cypher guard.
- `tests/test_mcp_integration.py` loads a synthetic corpus (`tests/mcp_fixture.py`)
  with player, DM-only, private, and two-campaign material into a disposable
  Neo4j and checks leakage per audience and search mode, hidden-id behavior,
  rejected assertions, campaign isolation, citations, paths, timeouts,
  read-only enforcement, embedder fallback, and the MCP protocol surface. It runs
  only when `LOREGRAPH_TEST_NEO4J_URI` and `LOREGRAPH_TEST_NEO4J_PASSWORD` are
  set, because loading replaces LoreGraph-managed nodes in that database.

## 10. Limitations and future work

- **Entity-level visibility.** Visibility is derived from evidence. Aliases
  and names of a visible entity may still have been learned from restricted
  sources. Adding explicit entity visibility to the ledger would close this.
- **Post-filtered vector recall.** Heavily restricted audiences can see fewer
  than `limit` vector hits. Neo4j's filtered vector search (`SEARCH` clause,
  2025.x) could replace over-fetching once the minimum supported version moves.
- **Assertions without evidence.** Manifest-declared assertions have no
  `SUPPORTED_BY` fragment in the projection; they are returned with their
  derivation method and an empty evidence list.
- **MCP resources and prompts.** Citation-addressable resources
  (`loregraph://chunk/{id}`) and journey prompts (lore question, session recap,
  open decisions) are natural next additions.
- **Journey planners.** TDD §8 describes lore, campaign, and notes planners
  with evidence-quality reranking; the current tools expose the filters those
  planners need, and a `build_context_pack` tool could compose them under a
  token budget.

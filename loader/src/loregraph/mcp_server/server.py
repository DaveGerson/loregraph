"""MCP tool surface for LoreGraph.

Tools are thin, typed wrappers around :class:`LoreGraphReader`. Every tool is
read-only; access policy is fixed when the server starts and can only be
narrowed by tool arguments.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypeVar

from pydantic import Field

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("Install the MCP extra: pip install -e '.[mcp]'") from exc

from .graph import GraphError
from .retrieval import LoreGraphReader
from .settings import ServerSettings, SettingsError

T = TypeVar("T")

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

Connection = Annotated[
    str | None,
    Field(description="Named Neo4j connection from the server config. Omit for the default."),
]
CampaignId = Annotated[
    str | None,
    Field(
        description=(
            "Limit campaign-scoped records to this campaign id (e.g. 'campaign:alpha'). "
            "Setting lore, rules, and notes without a campaign still match unless "
            "canon_scopes excludes them. List ids with graph_schema."
        )
    ),
]
CanonScope = Literal[
    "SETTING_SOURCE", "ADVENTURE_SOURCE", "CAMPAIGN", "HOMEBREW", "REFERENCE", "PERSONAL", "NONE"
]
Actuality = Literal["AUTHORED", "PLANNED", "OCCURRED", "HYPOTHETICAL", "SUPERSEDED"]
TruthStatus = Literal["ASSERTED", "DISPUTED", "UNKNOWN", "FALSE", "RETRACTED"]
ReviewStatus = Literal["ACCEPTED", "UNREVIEWED", "NEEDS_REVIEW", "REJECTED"]
EntityRef = Annotated[
    str,
    Field(
        description=(
            "Entity id (urn:...) or a name/alias. Names resolve to the best visible match; "
            "the response's resolution block shows how."
        ),
        min_length=1,
        max_length=300,
    ),
]


def build_server(
    settings: ServerSettings, reader: LoreGraphReader, *, log_level: str = "WARNING"
) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {}
        finally:
            reader.gateway.close()

    app = MCPServer(
        name="loregraph",
        title="LoreGraph",
        description="Read-only, citation-first access to a LoreGraph Neo4j projection.",
        instructions=instructions(settings),
        version=reader.server_version,
        lifespan=lifespan,
        log_level=log_level,  # type: ignore[arg-type]
    )

    @app.tool(title="Connection and index status", annotations=READ_ONLY)
    def status(connection: Connection = None) -> dict[str, Any]:
        """Check the Neo4j connection, server version, access policy, and which search modes
        (full-text, vector) are available. Call this first if another tool fails."""

        return _call(reader.status, connection=connection)

    @app.tool(title="Describe the visible graph", annotations=READ_ONLY)
    def graph_schema(connection: Connection = None) -> dict[str, Any]:
        """Summarize what this agent may see: entity types, predicates in use (with domain and
        range), campaign ids, visible counts, and the LoreGraph data dictionary explaining
        truth, review, actuality, scope, and visibility fields."""

        return _call(reader.graph_schema, connection=connection)

    @app.tool(title="Search passages", annotations=READ_ONLY)
    def search_passages(
        query: Annotated[
            str, Field(description="Question or keywords.", min_length=1, max_length=2000)
        ],
        mode: Annotated[
            Literal["auto", "fulltext", "vector", "hybrid"],
            Field(description="auto uses hybrid when vector search is available, else full-text."),
        ] = "auto",
        limit: Annotated[int, Field(ge=1, le=25)] = 8,
        campaign_id: CampaignId = None,
        canon_scopes: Annotated[
            list[CanonScope] | None, Field(description="Only these canon scopes.")
        ] = None,
        source_codes: Annotated[
            list[str] | None, Field(description="Only these source codes (see list_sources).")
        ] = None,
        content_types: Annotated[
            list[str] | None, Field(description="e.g. lore, rules, statblock, adventure, item.")
        ] = None,
        include_superseded: Annotated[
            bool, Field(description="Also search chunks from superseded source versions.")
        ] = False,
        max_chars: Annotated[
            int, Field(ge=200, le=8000, description="Text budget per passage.")
        ] = 1500,
        connection: Connection = None,
    ) -> dict[str, Any]:
        """Find source passages relevant to a question using full-text and/or vector search.
        Each result includes the passage text, a ready-to-use citation (source, edition, pages,
        heading), mentioned entities, and neighbor chunk ids for get_passage."""

        return _call(
            reader.search_passages,
            query,
            mode=mode,
            limit=limit,
            campaign_id=campaign_id,
            canon_scopes=canon_scopes,
            source_codes=source_codes,
            content_types=content_types,
            include_superseded=include_superseded,
            max_chars=max_chars,
            connection=connection,
        )

    @app.tool(title="Read a passage with context", annotations=READ_ONLY)
    def get_passage(
        chunk_id: Annotated[
            str, Field(description="Chunk id from a search or citation.", min_length=1)
        ],
        neighbors: Annotated[
            int, Field(ge=0, le=3, description="Adjacent chunks to include on each side.")
        ] = 1,
        max_chars: Annotated[
            int, Field(ge=200, le=20000, description="Text budget per passage.")
        ] = 6000,
        connection: Connection = None,
    ) -> dict[str, Any]:
        """Read one passage in full plus its neighboring chunks (reading order preserved)."""

        return _call(
            reader.get_passage,
            chunk_id,
            neighbors=neighbors,
            max_chars=max_chars,
            connection=connection,
        )

    @app.tool(title="Search entities", annotations=READ_ONLY)
    def search_entities(
        query: Annotated[
            str, Field(description="Name, alias, or partial name.", min_length=1, max_length=300)
        ],
        entity_types: Annotated[
            list[str] | None,
            Field(description="Only these types, e.g. Character, Faction, Location, Project."),
        ] = None,
        campaign_id: CampaignId = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        connection: Connection = None,
    ) -> dict[str, Any]:
        """Find people, places, factions, projects, rules elements, and other entities by name or
        alias (typo tolerant). Returns ids for get_entity, find_assertions, and find_connections."""

        return _call(
            reader.search_entities,
            query,
            entity_types=entity_types,
            campaign_id=campaign_id,
            limit=limit,
            connection=connection,
        )

    @app.tool(title="Entity profile", annotations=READ_ONLY)
    def get_entity(
        entity: EntityRef,
        campaign_id: CampaignId = None,
        assertion_limit: Annotated[int, Field(ge=0, le=100)] = 20,
        passage_limit: Annotated[int, Field(ge=0, le=20)] = 5,
        connection: Connection = None,
    ) -> dict[str, Any]:
        """Profile one entity: identity and aliases, a count of its assertions by predicate, the
        strongest assertions with quoted evidence, and passages that mention it."""

        return _call(
            reader.get_entity,
            entity,
            campaign_id=campaign_id,
            assertion_limit=assertion_limit,
            passage_limit=passage_limit,
            connection=connection,
        )

    @app.tool(title="Find assertions", annotations=READ_ONLY)
    def find_assertions(
        entity: Annotated[
            str | None,
            Field(description="Entity id or name the assertions involve.", max_length=300),
        ] = None,
        predicates: Annotated[
            list[str] | None, Field(description="Predicate codes, e.g. MEMBER_OF, LOCATED_IN.")
        ] = None,
        direction: Annotated[
            Literal["any", "outgoing", "incoming"],
            Field(description="outgoing: entity is the subject; incoming: entity is the object."),
        ] = "any",
        other_entity: Annotated[
            str | None,
            Field(description="Only assertions that also involve this entity.", max_length=300),
        ] = None,
        campaign_id: CampaignId = None,
        canon_scopes: list[CanonScope] | None = None,
        actualities: Annotated[
            list[Actuality] | None,
            Field(description="Use ['OCCURRED'] for what actually happened in play."),
        ] = None,
        truth_statuses: list[TruthStatus] | None = None,
        review_statuses: Annotated[
            list[ReviewStatus] | None,
            Field(description="Use ['ACCEPTED'] for reviewed facts only."),
        ] = None,
        include_superseded: bool = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        connection: Connection = None,
    ) -> dict[str, Any]:
        """Query the authoritative claims (subject, predicate, object) with truth status, review
        status, plan-vs-play actuality, scope, confidence, and exact supporting quotes with
        citations. Filter by entity, predicate, campaign, or any claim dimension."""

        return _call(
            reader.find_assertions,
            entity=entity,
            predicates=predicates,
            direction=direction,
            other_entity=other_entity,
            campaign_id=campaign_id,
            canon_scopes=canon_scopes,
            actualities=actualities,
            truth_statuses=truth_statuses,
            review_statuses=review_statuses,
            include_superseded=include_superseded,
            limit=limit,
            connection=connection,
        )

    @app.tool(title="How are two entities connected?", annotations=READ_ONLY)
    def find_connections(
        source: EntityRef,
        target: EntityRef,
        max_hops: Annotated[int, Field(ge=1, le=4)] = 3,
        limit: Annotated[int, Field(ge=1, le=10)] = 3,
        campaign_id: CampaignId = None,
        connection: Connection = None,
    ) -> dict[str, Any]:
        """Find the shortest chains of permitted assertions linking two entities, each step with
        its predicate, direction, status, and one supporting quote."""

        return _call(
            reader.find_connections,
            source,
            target,
            max_hops=max_hops,
            limit=limit,
            campaign_id=campaign_id,
            connection=connection,
        )

    @app.tool(title="List sources", annotations=READ_ONLY)
    def list_sources(
        source_kinds: Annotated[
            list[str] | None,
            Field(description="e.g. setting, rules, adventure, campaign, note, document, web."),
        ] = None,
        campaign_id: CampaignId = None,
        include_superseded: bool = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        connection: Connection = None,
    ) -> dict[str, Any]:
        """List visible source versions (books, notes, logs) with code, edition, rights
        attribution, and chunk counts. Use codes to scope search_passages."""

        return _call(
            reader.list_sources,
            source_kinds=source_kinds,
            campaign_id=campaign_id,
            include_superseded=include_superseded,
            limit=limit,
            connection=connection,
        )

    if settings.allow_raw_cypher:

        @app.tool(title="Read-only Cypher (owner)", annotations=READ_ONLY)
        def read_cypher(
            query: Annotated[
                str, Field(description="One read-only Cypher statement.", min_length=1)
            ],
            parameters: Annotated[
                dict[str, Any] | None, Field(description="Query parameters referenced as $name.")
            ] = None,
            limit: Annotated[int, Field(ge=1, le=1000)] = 100,
            connection: Connection = None,
        ) -> dict[str, Any]:
            """Run one read-only Cypher statement for questions the other tools cannot answer.
            Always pass user values through parameters. Writes, LOAD CSV, and procedures
            outside a read-only allow-list are rejected; call graph_schema first."""

            return _call(
                reader.read_cypher, query, parameters=parameters, limit=limit, connection=connection
            )

    return app


def instructions(settings: ServerSettings) -> str:
    policy = settings.policy
    return f"""LoreGraph is a provenance-first knowledge graph of lore, tabletop campaigns, rules,
and personal notes. Every claim is an Assertion backed by quoted evidence from a specific source
version and page.

Suggested workflow:
1. search_passages for open questions; search_entities to resolve names to ids.
2. get_entity, find_assertions, or find_connections for structured facts about entities.
3. get_passage to read surrounding context before quoting.
4. Cite with the `citation` strings provided; include attribution text when present.

Interpretation rules:
- review_status UNREVIEWED or NEEDS_REVIEW means machine-extracted and unverified; say so.
- truth_status DISPUTED/UNKNOWN/FALSE/RETRACTED and competing assertions must not be flattened
  into one fact. Present disagreements with their sources.
- actuality PLANNED or AUTHORED (adventure prep) is not campaign history; OCCURRED is.
- campaign_id isolates campaigns; never merge facts across campaigns.

Access policy (fixed by the operator): audience '{policy.audience}', visibilities
{", ".join(policy.visibilities)}. Results are filtered before search, so a missing result does
not prove a fact is absent from the corpus. Do not speculate about withheld material."""


def _call(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return function(*args, **kwargs)
    except (
        GraphError,
        SettingsError,
        LookupError,
        PermissionError,
        ValueError,
        RuntimeError,
    ) as exc:
        raise ToolError(str(exc)) from exc

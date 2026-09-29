"""Pure helpers for building policy-filtered Cypher and shaping results.

Nothing here talks to Neo4j, so every guard can be unit tested offline. All
user-supplied values travel as query parameters; only fixed fragments and
validated integers are interpolated into query text.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

# Loader-managed nodes that also carry the generic :Entity label but are part of
# the provenance kernel rather than domain identities.
KERNEL_LABELS = (
    "Material",
    "MaterialVersion",
    "SourceFragment",
    "Assertion",
    "LexicalEntry",
    "LiteralValue",
    "KnowledgeChunk",
    "Embedding",
)
# Chunks from NO_INDEX / METADATA_ONLY sources must never yield text, even if a
# hand-edited graph contains them.
TEXT_INDEXING_MODES = ("PRIVATE_ONLY", "FULL_TEXT", "FULL_TEXT_WITH_ATTRIBUTION")
VECTOR_PROPERTIES = frozenset({"embedding", "vector"})


def chunk_filter(var: str) -> str:
    """Predicate for a KnowledgeChunk the current policy may read."""

    return (
        f"{var}.visibility IN $visibilities"
        f" AND {var}.indexingMode IN $textIndexingModes"
        f" AND ($includeSuperseded OR {var}.status = 'ACTIVE')"
        f" AND (size($canonScopes) = 0 OR {var}.canonScope IN $canonScopes)"
        f" AND ({var}.campaignId IS NULL OR size($campaignIds) = 0"
        f" OR {var}.campaignId IN $campaignIds)"
    )


def assertion_filter(var: str) -> str:
    """Predicate for an Assertion the current policy may read."""

    return (
        f"{var}.visibility IN $visibilities"
        f" AND {var}.reviewStatus IN $reviewStatuses"
        f" AND ($includeSuperseded OR {var}.active = true)"
        f" AND (size($canonScopes) = 0 OR {var}.canonScope IN $canonScopes)"
        f" AND ({var}.campaignId IS NULL OR size($campaignIds) = 0"
        f" OR {var}.campaignId IN $campaignIds)"
    )


def domain_entity(var: str) -> str:
    labels = " OR ".join(f"{var}:{label}" for label in KERNEL_LABELS)
    return f"{var}:Entity AND NOT ({labels})"


def entity_filter(var: str) -> str:
    """Predicate for a domain entity visible under the current policy.

    Entity nodes carry no visibility of their own, so an entity is visible only
    when an allowed chunk mentions it or an allowed assertion references it.
    Identity seeds that no permitted evidence touches stay hidden.
    """

    chunk_var = f"{var}_chunk"
    assertion_var = f"{var}_assertion"
    return (
        f"{domain_entity(var)}"
        f" AND ({var}.campaignId IS NULL OR size($campaignIds) = 0"
        f" OR {var}.campaignId IN $campaignIds)"
        f" AND (EXISTS {{ MATCH ({chunk_var}:KnowledgeChunk)-[:MENTIONS]->({var})"
        f" WHERE {chunk_filter(chunk_var)} }}"
        f" OR EXISTS {{ MATCH ({assertion_var}:Assertion)-[:SUBJECT|OBJECT]->({var})"
        f" WHERE {assertion_filter(assertion_var)} }})"
    )


_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/&|])')
_WORD = re.compile(r"[\w'’-]+", re.UNICODE)
_LUCENE_OPERATORS = {"and", "or", "not", "to"}


def lucene_query(text: str, *, fuzzy: bool = False, max_terms: int = 16) -> str:
    """Turn free text into a safe Lucene query for Neo4j full-text indexes.

    The exact phrase is boosted, every term is optional, and all Lucene syntax
    in the input is escaped, so agent text can never raise a parser error.
    """

    terms = []
    for token in _WORD.findall(text):
        token = token.strip("'’-")
        if token and token.casefold() not in _LUCENE_OPERATORS:
            terms.append(token)
    terms = list(dict.fromkeys(terms))[:max_terms]
    if not terms:
        raise ValueError("The query must contain at least one searchable word")
    escaped = [_LUCENE_SPECIAL.sub(r"\\\1", term) for term in terms]
    clauses = []
    if len(escaped) > 1:
        clauses.append('"' + " ".join(escaped) + '"^4')
    for term in escaped:
        clauses.append(term)
        if fuzzy and len(term) >= 5:
            clauses.append(f"{term}~1")
    return " OR ".join(clauses)


def name_similarity(query: str, names: list[str]) -> float:
    """How well ``query`` names one of ``names`` (1.0 exact, 0.9 token subset, else ratio)."""

    def normalize(value: str) -> str:
        return " ".join(re.findall(r"\w+", value.casefold()))

    wanted = normalize(query)
    best = 0.0
    for name in names:
        candidate = normalize(name or "")
        if not candidate:
            continue
        if wanted == candidate:
            return 1.0
        if set(wanted.split()) <= set(candidate.split()):
            best = max(best, 0.9)
        best = max(best, SequenceMatcher(None, wanted, candidate).ratio())
    return best


def reciprocal_rank_fusion(
    rankings: dict[str, list[str]], *, k: int = 60
) -> list[tuple[str, float, list[str]]]:
    """Fuse ranked id lists; returns (id, fused score, contributing rankers)."""

    scores: dict[str, float] = {}
    sources: dict[str, list[str]] = {}
    for ranker, ids in rankings.items():
        for rank, item in enumerate(ids, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            sources.setdefault(item, []).append(ranker)
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    return [(item, round(score, 6), sources[item]) for item, score in ordered]


def page_label(start: Any, end: Any) -> str | None:
    if start is None:
        return None
    if end is None or end == start:
        return f"p. {start}"
    return f"pp. {start}-{end}"


def citation(source: dict[str, Any] | None, pages: str | None, heading: str | None) -> str:
    source = source or {}
    title = source.get("title") or source.get("code") or "Unknown source"
    edition = source.get("edition")
    parts = [f"{title} ({edition})" if edition else str(title)]
    if pages:
        parts.append(pages)
    text = ", ".join(parts)
    if heading:
        text += f" — {heading}"
    if source.get("current") is False:
        text += " [superseded version]"
    if source.get("indexing_mode") == "FULL_TEXT_WITH_ATTRIBUTION" and source.get("attribution"):
        text += f". {source['attribution']}"
    return text


def truncate(text: str | None, max_chars: int) -> tuple[str, bool]:
    if not text:
        return "", False
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    boundary = cut.rfind(" ")
    if boundary > max_chars * 0.8:
        cut = cut[:boundary]
    return cut.rstrip() + " …", True


def snippet(text: str, needle: str | None, radius: int = 220) -> str:
    """Window of text around the first occurrence of ``needle``."""

    if not text:
        return ""
    position = text.casefold().find(needle.casefold()) if needle else -1
    if position < 0:
        return truncate(text, radius * 2)[0]
    start = max(0, position - radius)
    end = min(len(text), position + len(needle or "") + radius)
    prefix = "… " if start > 0 else ""
    suffix = " …" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


# ---------------------------------------------------------------------------
# Raw Cypher guard (owner audience only). Read-only transactions and EXPLAIN
# query-type checks are the primary controls; these textual rules block reads
# that reach outside the database (files, URLs, nested dynamic Cypher).

_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_PROCEDURE_CALL = re.compile(r"\bCALL\s+([A-Za-z_][\w]*(?:\s*\.\s*[A-Za-z_][\w]*)+)", re.IGNORECASE)
_FORBIDDEN_TEXT = (
    (re.compile(r"\bLOAD\s+CSV\b", re.IGNORECASE), "LOAD CSV reads files or URLs"),
    (re.compile(r"\bUSE\s+[A-Za-z_]", re.IGNORECASE), "USE switches databases"),
    (
        re.compile(
            r"\bapoc\s*\.\s*(load|import|export|cypher|periodic|trigger|custom|systemdb|util|static|log|http)\b",
            re.IGNORECASE,
        ),
        "this APOC namespace can reach outside the read-only graph",
    ),
    (re.compile(r"\b(gds|dbms)\s*\.", re.IGNORECASE), "admin and GDS procedures are not exposed"),
)
_ALLOWED_PROCEDURE_PREFIXES = (
    "db.labels",
    "db.relationshiptypes",
    "db.propertykeys",
    "db.schema.",
    "db.index.fulltext.querynodes",
    "db.index.fulltext.queryrelationships",
    "db.index.vector.querynodes",
    "db.awaitindex",
    "apoc.meta.",
    "apoc.path.",
    "apoc.neighbors.",
    "apoc.nodes.",
    "apoc.coll.",
    "apoc.map.",
    "apoc.text.",
    "apoc.convert.",
)


def check_raw_cypher(query: str) -> str:
    """Validate agent-supplied Cypher text; returns the stripped query."""

    text = query.strip().rstrip(";").strip()
    if not text:
        raise ValueError("The query is empty")
    normalized = _COMMENTS.sub(" ", text).replace("`", "")
    if ";" in normalized:
        raise ValueError("Submit exactly one statement")
    if re.match(r"^\s*(EXPLAIN|PROFILE)\b", normalized, re.IGNORECASE):
        raise ValueError("Omit EXPLAIN/PROFILE; the server plans the query itself")
    for pattern, reason in _FORBIDDEN_TEXT:
        if pattern.search(normalized):
            raise ValueError(f"Query rejected: {reason}")
    for match in _PROCEDURE_CALL.finditer(normalized):
        name = re.sub(r"\s+", "", match.group(1)).casefold()
        if not name.startswith(_ALLOWED_PROCEDURE_PREFIXES):
            raise ValueError(f"Procedure {name} is not on the read-only allow-list")
    return text


# ---------------------------------------------------------------------------
# Static data dictionary returned by graph_schema so agents can reason about
# the projection without reading the loader source.

DATA_DICTIONARY: dict[str, Any] = {
    "source_chain": {
        "Material": "A conceptual work (book, note set, campaign log).",
        "MaterialVersion": "One edition/file of a Material; carries rights, visibility, scope, "
        "and a current flag. Linked by (:Material)-[:CURRENT_VERSION]->().",
        "SourceFragment": "A citable address: a page range or an exact TEXT_QUOTE selector.",
        "KnowledgeChunk": "Retrieval unit with text, headingPath, pageStart/pageEnd, status "
        "(ACTIVE|SUPERSEDED), and NEXT_CHUNK links to adjacent context.",
    },
    "identity": {
        "Entity": "Every domain identity has :Entity plus a type label such as :Character, "
        ":Faction, :Location, :Project, or :Decision. Names and aliases live on the node.",
        "MENTIONS": "(:KnowledgeChunk)-[:MENTIONS {surface}]->(:Entity) deterministic mentions.",
    },
    "assertions": {
        "Assertion": "The authoritative claim: (a)-[:SUBJECT]->(subject), (a)-[:OBJECT]->(object) "
        "or (a)-[:LITERAL_OBJECT]->(:LiteralValue), (a)-[:SUPPORTED_BY]->(:SourceFragment)"
        "-[:EVIDENCE_IN_CHUNK]->(:KnowledgeChunk).",
        "projection_edges": "Optional direct edges named by predicate code (e.g. MEMBER_OF) that "
        "carry assertionId and are rebuildable; prefer Assertion nodes for filtering.",
    },
    "dimensions": {
        "truthStatus": "ASSERTED | DISPUTED | UNKNOWN | FALSE | RETRACTED",
        "reviewStatus": "ACCEPTED | UNREVIEWED | NEEDS_REVIEW | REJECTED (model output starts "
        "UNREVIEWED; treat it as unverified)",
        "actuality": "AUTHORED | PLANNED | OCCURRED | HYPOTHETICAL | SUPERSEDED (plans are not "
        "history)",
        "canonScope": "SETTING_SOURCE | ADVENTURE_SOURCE | CAMPAIGN | HOMEBREW | REFERENCE | "
        "PERSONAL | NONE",
        "visibility": "PUBLIC | PLAYER | DM_ONLY | PRIVATE",
        "campaignId": "Present on campaign-scoped records; campaigns never share identities.",
        "validFrom/validTo": "In-world or real valid time, separate from record time.",
    },
}

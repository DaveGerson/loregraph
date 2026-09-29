"""Synthetic multi-audience corpus for exercising the MCP read server.

The corpus deliberately mixes player-visible lore, DM-only secrets, private
notes, and two isolated campaigns so leakage and isolation rules can be tested
against a real Neo4j projection. Assertions are injected as if an extraction
run had produced them, with grounded quotes, so the ledger still passes audit.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

import yaml

from loregraph.config import LoaderConfig
from loregraph.ids import normalize_text, sha256_bytes, stable_id
from loregraph.pipeline import ingest_manifest
from loregraph.store import RecordStore

ROOT = Path(__file__).resolve().parents[1]
EMBEDDING_MODEL = "test-hashing-64"
EMBEDDING_DIMENSIONS = 64

SOURCES: dict[str, tuple[dict[str, Any], str]] = {
    "setting.md": (
        {
            "code": "SETTING",
            "title": "Gazetteer of Sharn",
            "edition": "v1",
            "source_kind": "setting",
            "canon_scope": "SETTING_SOURCE",
            "actuality": "AUTHORED",
            "rights_basis": "CC_BY_4_0",
            "indexing_mode": "FULL_TEXT_WITH_ATTRIBUTION",
            "attribution": "Gazetteer of Sharn, CC-BY-4.0, Example Press",
            "visibility": "PLAYER",
        },
        "# Gazetteer\n\n<!-- page: 1 -->\n\n## The City of Towers\n\n"
        "Sharn is a vertical city of impossible spires. Lightning rail lines and "
        "airships connect its towers to the rest of Breland. "
        * 3
        + "\n\n<!-- page: 2 -->\n\n## Harbormasters\n\n"
        "Captain Veyra is a member of House Lyrandar. House Lyrandar operates in "
        "Sharn and controls the airship docks. Storms follow the house airships. " * 3,
    ),
    "secrets.md": (
        {
            "code": "SECRETS",
            "title": "Secrets of the Towers",
            "edition": "v1",
            "source_kind": "setting",
            "canon_scope": "SETTING_SOURCE",
            "actuality": "AUTHORED",
            "rights_basis": "PRIVATE_PLAY",
            "indexing_mode": "PRIVATE_ONLY",
            "visibility": "DM_ONLY",
        },
        "# Secrets\n\n<!-- page: 7 -->\n\n## The Double Agent\n\n"
        "Captain Veyra is secretly a member of House Cannith. The stolen manifest "
        "was copied by Cannith artificers beneath Sharn. " * 3,
    ),
    "campaign-alpha.md": (
        {
            "code": "ALPHA-LOG",
            "title": "Alpha session log",
            "edition": "s1",
            "source_kind": "campaign",
            "canon_scope": "CAMPAIGN",
            "actuality": "OCCURRED",
            "rights_basis": "USER_OWNED",
            "indexing_mode": "FULL_TEXT",
            "visibility": "PLAYER",
            "campaign_id": "campaign:alpha",
        },
        "# Session one\n\n<!-- page: 1 -->\n\n## Arrival\n\n"
        "Mira is located in Sharn after the party rode the lightning rail from "
        "Breland's interior. The party met a Lyrandar courier. " * 3,
    ),
    "campaign-beta.md": (
        {
            "code": "BETA-LOG",
            "title": "Beta session log",
            "edition": "s1",
            "source_kind": "campaign",
            "canon_scope": "CAMPAIGN",
            "actuality": "OCCURRED",
            "rights_basis": "USER_OWNED",
            "indexing_mode": "FULL_TEXT",
            "visibility": "PLAYER",
            "campaign_id": "campaign:beta",
        },
        "# Session one\n\n<!-- page: 1 -->\n\n## Oath\n\n"
        "Doran is a member of Thrane and swore a templar oath in Flamekeep. "
        "The Silver Flame blessed the oath. " * 3,
    ),
    "notes.md": (
        {
            "code": "NOTES",
            "title": "Prep notes",
            "edition": "v1",
            "source_kind": "note",
            "canon_scope": "PERSONAL",
            "actuality": "AUTHORED",
            "rights_basis": "USER_OWNED",
            "indexing_mode": "FULL_TEXT",
            "visibility": "PRIVATE",
        },
        "# Prep\n\n## Reminders\n\nRemember that the zeppelin heist happens next month. "
        "Buy more dice before the zeppelin heist session. " * 3,
    ),
}


def hashing_embedding(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Deterministic bag-of-words embedding so vector search is testable offline."""

    vector = [0.0] * dimensions
    for token in re.findall(r"[a-z]+", text.casefold()):
        if len(token) < 3:
            continue
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:4], "big") % dimensions] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class HashingEmbedder:
    provider = "test"
    model = EMBEDDING_MODEL

    def embed(self, text: str) -> list[float]:
        return hashing_embedding(text)


def write_config(root: Path) -> Path:
    config_file = root / "project.toml"
    config_file.write_text(
        f"""
[project]
tenant_id = "test"
workspace_dir = "{(root / "workspace").as_posix()}"
schema_path = "{(ROOT / "schema/canonical-profile.json").as_posix()}"
backbone_path = "{(ROOT / "config/backbone.eberron.json").as_posix()}"

[chunking]
target_tokens = 60
max_tokens = 90
min_tokens = 1
atomic_max_tokens = 1000
overlap_tokens = 0

[embeddings]
provider = "none"
model = "{EMBEDDING_MODEL}"
vector_index_name = "chunk_embedding"

[neo4j]
create_projection_edges = true
project_unreviewed = true
""".strip(),
        encoding="utf-8",
    )
    return config_file


def build_ledger(root: Path) -> LoaderConfig:
    """Ingest the synthetic sources and inject assertions and embeddings."""

    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for filename, (spec, text) in SOURCES.items():
        (root / filename).write_text(text, encoding="utf-8")
        rows.append({"path": filename, **spec})
    manifest = root / "sources.yaml"
    manifest.write_text(yaml.safe_dump({"sources": rows}, sort_keys=False), encoding="utf-8")
    config = LoaderConfig.from_toml(write_config(root))
    ingest_manifest(config, manifest)

    store = RecordStore(config.workspace_dir)
    sources = {row["code"]: row for row in store.read("sources")}
    chunks = store.read("chunks")
    entities = store.by_id("entities")

    def chunk_with(code: str, needle: str) -> dict[str, Any]:
        for chunk in chunks:
            if chunk["source_id"] == sources[code]["id"] and needle in chunk["raw_text"]:
                return chunk
        raise AssertionError(f"No {code} chunk contains {needle!r}")

    def entity(
        entity_type: str, name: str, chunk: dict[str, Any], campaign_id: str | None = None
    ) -> str:
        entity_id = stable_id("test", entity_type.casefold(), name, campaign_id or "global")
        entities[entity_id] = {
            "id": entity_id,
            "entity_type": entity_type,
            "subtype": "npc" if entity_type == "Character" else "other",
            "canonical_name": name,
            "aliases": [],
            "scope": "CAMPAIGN" if campaign_id else "GLOBAL",
            "campaign_id": campaign_id,
            "description": f"Secret dossier on {name}",
            "confidence": 0.9,
            "provisional": True,
            "source_chunk_ids": [chunk["id"]],
            "metadata": {"structured": {"note": "dm-only detail"}},
        }
        return entity_id

    evidence_rows = store.by_id("evidence")
    assertion_rows = store.by_id("assertions")
    mention_rows = store.by_id("mentions")

    def mention(chunk: dict[str, Any], entity_id: str, surface: str) -> None:
        start = chunk["raw_text"].find(surface)
        mention_id = stable_id("test", "mention", chunk["id"], entity_id, start)
        mention_rows[mention_id] = {
            "id": mention_id,
            "chunk_id": chunk["id"],
            "entity_id": entity_id,
            "surface": surface,
            "start": start,
            "end": start + len(surface),
            "method": "TEST",
            "confidence": 1.0,
        }

    def assertion(
        chunk: dict[str, Any],
        subject_id: str,
        predicate: str,
        object_id: str,
        quote: str,
        review_status: str = "UNREVIEWED",
    ) -> str:
        assert quote in chunk["raw_text"], quote
        normalized = normalize_text(quote)
        evidence_id = stable_id("global", "fragment", chunk["id"], normalized)
        evidence_rows[evidence_id] = {
            "id": evidence_id,
            "chunk_id": chunk["id"],
            "source_id": chunk["source_id"],
            "quote": quote,
            "page_start": chunk["page_start"],
            "page_end": chunk["page_end"],
            "exact_text_hash": sha256_bytes(normalized.encode("utf-8")),
        }
        assertion_id = stable_id("test", "assertion", chunk["id"], subject_id, predicate, object_id)
        assertion_rows[assertion_id] = {
            "id": assertion_id,
            "subject_id": subject_id,
            "predicate_code": predicate,
            "object_id": object_id,
            "literal_value": None,
            "truth_status": "ASSERTED",
            "canon_scope": chunk["canon_scope"],
            "actuality": chunk["actuality"],
            "confidence": 0.9,
            "review_status": review_status,
            "derivation_method": "LLM_EXTRACTION",
            "source_chunk_ids": [chunk["id"]],
            "evidence_ids": [evidence_id],
            "campaign_id": chunk.get("campaign_id"),
            "asserted_by_id": None,
            "valid_from": None,
            "valid_to": None,
            "visibility": chunk["visibility"],
            "metadata": {"source_quote": quote},
        }
        return assertion_id

    lyrandar = "urn:dndkg:global:faction:house-lyrandar"
    cannith = "urn:dndkg:global:faction:house-cannith"
    sharn = "urn:dndkg:global:location:sharn"
    thrane = "urn:dndkg:global:faction:thrane"

    harbor = chunk_with("SETTING", "Captain Veyra is a member of House Lyrandar.")
    veyra = entity("Character", "Captain Veyra", harbor)
    mention(harbor, veyra, "Captain Veyra")
    assertion(
        harbor,
        veyra,
        "MEMBER_OF",
        lyrandar,
        "Captain Veyra is a member of House Lyrandar.",
        "ACCEPTED",
    )
    assertion(harbor, lyrandar, "OPERATES_IN", sharn, "House Lyrandar operates in Sharn")

    secret = chunk_with("SECRETS", "Captain Veyra is secretly a member of House Cannith.")
    mention(secret, veyra, "Captain Veyra")
    assertion(
        secret, veyra, "MEMBER_OF", cannith, "Captain Veyra is secretly a member of House Cannith."
    )
    # A second secret-only character that must never surface for players.
    artificer = entity("Character", "Cannith Artificer Zell", secret)
    assertion(secret, artificer, "MEMBER_OF", cannith, "copied by Cannith artificers", "REJECTED")

    alpha = chunk_with("ALPHA-LOG", "Mira is located in Sharn")
    mira = entity("Character", "Mira", alpha, "campaign:alpha")
    mention(alpha, mira, "Mira")
    assertion(alpha, mira, "LOCATED_IN", sharn, "Mira is located in Sharn")

    beta = chunk_with("BETA-LOG", "Doran is a member of Thrane")
    doran = entity("Character", "Doran", beta, "campaign:beta")
    mention(beta, doran, "Doran")
    assertion(beta, doran, "MEMBER_OF", thrane, "Doran is a member of Thrane")

    store.replace("entities", entities.values())
    store.replace("mentions", mention_rows.values())
    store.replace("evidence", evidence_rows.values())
    store.replace("assertions", assertion_rows.values())

    embeddings = []
    for chunk in chunks:
        vector = hashing_embedding(chunk["text"])
        embeddings.append(
            {
                "id": stable_id("test", "embedding", chunk["id"], EMBEDDING_MODEL),
                "chunk_id": chunk["id"],
                "provider": "test",
                "model": EMBEDDING_MODEL,
                "dimensions": len(vector),
                "input_hash": chunk["text_hash"],
                "vector": vector,
                "active": True,
            }
        )
    store.replace("embeddings", embeddings)
    return config

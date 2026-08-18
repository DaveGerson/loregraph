from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from .audit import audit_store
from .config import LoaderConfig
from .ids import short_id, slugify
from .store import RecordStore


def export_obsidian(config: LoaderConfig) -> dict[str, int]:
    audit = audit_store(config)
    if not audit.ok:
        raise ValueError(f"Ledger audit failed; refusing Obsidian export: {audit.errors[:10]}")
    store = RecordStore(config.workspace_dir)
    vault = Path(config.obsidian.vault_path)
    dirs = {
        "dashboard": vault / "00 Dashboard",
        "sources": vault / "01 Sources",
        "entities": vault / "02 Entities",
        "assertions": vault / "03 Assertions",
        "chunks": vault / "04 Evidence Chunks",
        "data": vault / "_data",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)

    sources = store.read("sources")
    chunks = store.read("chunks")
    entities = store.read("entities")
    assertions = store.read("assertions")
    evidence = store.read("evidence")
    reviews = store.read("review_queue")
    if not config.obsidian.include_superseded:
        current_source_ids = {
            row["id"] for row in sources if row.get("metadata", {}).get("is_current", True)
        }
        sources = [row for row in sources if row["id"] in current_source_ids]
        current_chunk_ids = {row["id"] for row in chunks if row["source_id"] in current_source_ids}
        chunks = [row for row in chunks if row["id"] in current_chunk_ids]
        evidence = [row for row in evidence if row["chunk_id"] in current_chunk_ids]
        assertions = [
            row
            for row in assertions
            if not row.get("source_chunk_ids")
            or bool(current_chunk_ids.intersection(row.get("source_chunk_ids", [])))
        ]
        referenced_entity_ids = {
            endpoint
            for row in assertions
            for endpoint in (row.get("subject_id"), row.get("object_id"), row.get("asserted_by_id"))
            if endpoint
        }
        entities = [
            row
            for row in entities
            if referenced_entity_ids.intersection({row["id"]})
            or bool(current_chunk_ids.intersection(row.get("source_chunk_ids", [])))
        ]
        reviews = [
            row
            for row in reviews
            if not row.get("source_chunk_id") or row["source_chunk_id"] in current_chunk_ids
        ]
    source_paths = {row["id"]: _source_path(dirs["sources"], row) for row in sources}
    entity_paths = {row["id"]: _entity_path(dirs["entities"], row) for row in entities}
    assertion_paths = {
        row["id"]: _assertion_path(dirs["assertions"], row) for row in assertions
    }
    chunk_paths = {row["id"]: _chunk_path(dirs["chunks"], row) for row in chunks}
    chunk_index = {row["id"]: row for row in chunks}
    evidence_index = {row["id"]: row for row in evidence}
    entity_index = {row["id"]: row for row in entities}
    assertion_index = {row["id"]: row for row in assertions}

    for source in sources:
        related = [chunk for chunk in chunks if chunk["source_id"] == source["id"]]
        body = [
            f"# {source['title']}",
            "",
            f"Edition: {source['edition']}  ",
            f"Rights basis: {source['rights_basis']}  ",
            f"Indexing mode: {source['indexing_mode']}  ",
            f"Parser: {source['parser']}",
            "",
            "## Sections",
            "",
        ]
        sections = sorted({" > ".join(chunk.get("heading_path", [])) for chunk in related})
        body.extend(f"- {section or '(unheaded material)'}" for section in sections)
        frontmatter = {
            "id": source["id"],
            "type": "source",
            "code": source["code"],
            "edition": source["edition"],
            "canon_scope": source["canon_scope"],
            "actuality": source["actuality"],
            "rights_basis": source["rights_basis"],
            "indexing_mode": source["indexing_mode"],
            "visibility": source["visibility"],
            "campaign_id": source.get("campaign_id"),
            "game_system": source.get("game_system"),
            "ruleset": source.get("ruleset"),
            "ruleset_version": source.get("ruleset_version"),
            "remote_processing_allowed": source["remote_processing_allowed"],
            "tags": ["loregraph/source", f"loregraph/edition/{slugify(source['edition'])}"],
        }
        _write_note(source_paths[source["id"]], frontmatter, "\n".join(body))

    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for assertion in assertions:
        outgoing[assertion["subject_id"]].append(assertion)
        if assertion.get("object_id"):
            incoming[assertion["object_id"]].append(assertion)

    for entity in entities:
        source_ids = {
            chunk_index[chunk_id]["source_id"]
            for chunk_id in entity.get("source_chunk_ids", [])
            if chunk_id in chunk_index
        }
        source_links = [_wikilink(source_paths[sid], vault) for sid in sorted(source_ids)]
        body = [f"# {entity['canonical_name']}", ""]
        if entity.get("description"):
            body.extend([entity["description"], ""])
        body.extend(["## Relationships", ""])
        for assertion in outgoing.get(entity["id"], []):
            body.append(_relationship_line(assertion, entity_paths, entity_index, vault, outgoing=True))
        for assertion in incoming.get(entity["id"], []):
            body.append(_relationship_line(assertion, entity_paths, entity_index, vault, outgoing=False))
        if len(body) == 3:
            body.append("- No extracted assertions yet.")
        body.extend(["", "## Sources", ""])
        body.extend(f"- {link}" for link in source_links or ["No source mentions yet."])
        tags = [
            "loregraph/entity",
            f"loregraph/type/{slugify(entity['entity_type'])}",
            f"loregraph/subtype/{slugify(entity['subtype'])}",
        ]
        if entity.get("provisional"):
            tags.append("loregraph/review/provisional")
        frontmatter = {
            "id": entity["id"],
            "type": entity["entity_type"],
            "subtype": entity["subtype"],
            "aliases": entity.get("aliases", []),
            "scope": entity["scope"],
            "campaign_id": entity.get("campaign_id"),
            "provisional": entity.get("provisional", False),
            "sources": source_links,
            "tags": tags,
        }
        for key, value in entity.get("metadata", {}).get("structured", {}).items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                frontmatter[f"mechanic_{key}"] = value
        _write_note(entity_paths[entity["id"]], frontmatter, "\n".join(body))

    if config.obsidian.write_assertion_notes:
        for assertion in assertions:
            subject = entity_index.get(assertion["subject_id"])
            obj = entity_index.get(assertion.get("object_id"))
            subject_link = _wikilink(entity_paths[subject["id"]], vault) if subject else assertion["subject_id"]
            object_display = (
                _wikilink(entity_paths[obj["id"]], vault)
                if obj
                else assertion.get("literal_value") or "(missing object)"
            )
            quotes = [
                evidence_index[eid]
                for eid in assertion.get("evidence_ids", [])
                if eid in evidence_index
            ]
            body = [
                f"# {assertion['predicate_code']}",
                "",
                f"{subject_link} **{assertion['predicate_code']}** {object_display}",
                "",
                "## Evidence",
                "",
            ]
            for quote in quotes:
                excerpt = quote["quote"][: config.obsidian.max_quote_chars].replace("\n", " ")
                source_link = _wikilink(source_paths[quote["source_id"]], vault)
                body.extend([f"> {excerpt}", "", f"— {source_link}, pp. {quote['page_start']}-{quote['page_end']}", ""])
            frontmatter = {
                "id": assertion["id"],
                "type": "assertion",
                "predicate": assertion["predicate_code"],
                "subject": subject_link,
                "object": object_display,
                "truth_status": assertion["truth_status"],
                "canon_scope": assertion["canon_scope"],
                "actuality": assertion["actuality"],
                "campaign_id": assertion.get("campaign_id"),
                "confidence": assertion["confidence"],
                "review_status": assertion["review_status"],
                "visibility": assertion["visibility"],
                "tags": [
                    "loregraph/assertion",
                    f"loregraph/truth/{slugify(assertion['truth_status'])}",
                    f"loregraph/review/{slugify(assertion['review_status'])}",
                ],
            }
            _write_note(assertion_paths[assertion["id"]], frontmatter, "\n".join(body))

    if config.obsidian.write_chunk_notes:
        for chunk in chunks:
            source_link = _wikilink(source_paths[chunk["source_id"]], vault)
            body = f"# Evidence chunk {chunk['chunk_index']}\n\n{chunk['text']}\n\nSource: {source_link}"
            frontmatter = {
                "id": chunk["id"],
                "type": "knowledge-chunk",
                "source": source_link,
                "pages": f"{chunk['page_start']}-{chunk['page_end']}",
                "content_type": chunk["content_type"],
                "campaign_id": chunk.get("campaign_id"),
                "game_system": chunk.get("game_system"),
                "ruleset": chunk.get("ruleset"),
                "ruleset_version": chunk.get("ruleset_version"),
                "visibility": chunk["visibility"],
                "tags": ["loregraph/chunk", f"loregraph/content/{slugify(chunk['content_type'])}"],
            }
            _write_note(chunk_paths[chunk["id"]], frontmatter, body)

    dashboard = [
        "# LoreGraph Knowledge Base",
        "",
        "Generated from the portable LoreGraph evidence ledger.",
        "",
        f"- Sources: {len(sources)}",
        f"- Entities: {len(entities)}",
        f"- Assertions: {len(assertions)}",
        f"- Open review items: {sum(row.get('status') == 'OPEN' for row in reviews)}",
        "",
        "## Sources",
        "",
    ]
    dashboard.extend(f"- {_wikilink(source_paths[row['id']], vault)}" for row in sources)
    _write_note(
        dirs["dashboard"] / "Knowledge Graph Home.md",
        {"type": "dashboard", "tags": ["loregraph/dashboard"]},
        "\n".join(dashboard),
    )
    _write_machine_manifest(dirs["data"] / "export-manifest.yaml", sources, entities, assertions)
    current_generated = {
        *source_paths.values(),
        *entity_paths.values(),
        *(
            assertion_paths.values()
            if config.obsidian.write_assertion_notes
            else []
        ),
        *(
            chunk_paths.values()
            if config.obsidian.write_chunk_notes
            else []
        ),
        dirs["dashboard"] / "Knowledge Graph Home.md",
    }
    _clean_stale_generated(vault, dirs["data"] / "generated-files.txt", current_generated)
    return {
        "sources": len(sources),
        "entities": len(entities),
        "assertions": len(assertions),
        "chunks": len(chunks) if config.obsidian.write_chunk_notes else 0,
    }


def _relationship_line(
    assertion: dict[str, Any],
    paths: dict[str, Path],
    entities: dict[str, dict[str, Any]],
    vault: Path,
    outgoing: bool,
) -> str:
    other_id = assertion.get("object_id") if outgoing else assertion["subject_id"]
    other = entities.get(other_id)
    other_text = _wikilink(paths[other_id], vault) if other and other_id in paths else str(other_id)
    predicate = assertion["predicate_code"] if outgoing else f"← {assertion['predicate_code']}"
    qualifier = f"{assertion['truth_status'].lower()}, {assertion['review_status'].lower()}"
    return f"- **{predicate}** {other_text} ({qualifier})"


def _source_path(root: Path, row: dict[str, Any]) -> Path:
    return root / f"{slugify(row['code'])}--{short_id(row['id'])}.md"


def _entity_path(root: Path, row: dict[str, Any]) -> Path:
    folder = root / slugify(row["entity_type"])
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{slugify(row['canonical_name'])}--{short_id(row['id'])}.md"


def _assertion_path(root: Path, row: dict[str, Any]) -> Path:
    return root / f"{slugify(row['predicate_code'])}--{short_id(row['id'])}.md"


def _chunk_path(root: Path, row: dict[str, Any]) -> Path:
    return root / f"chunk-{row['chunk_index']:05d}--{short_id(row['id'])}.md"


def _wikilink(path: Path, vault: Path) -> str:
    relative = path.relative_to(vault).with_suffix("")
    return f"[[{relative.as_posix()}]]"


def _write_note(path: Path, properties: dict[str, Any], body: str) -> None:
    properties = {
        key: value
        for key, value in {"generated_by": "loregraph-loader", **properties}.items()
        if value is not None
    }
    frontmatter = yaml.safe_dump(
        properties,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    content = f"---\n{frontmatter}\n---\n\n{body.rstrip()}\n"
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8", newline="\n")
    temp.replace(path)


def _write_machine_manifest(
    path: Path,
    sources: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    assertions: list[dict[str, Any]],
) -> None:
    payload = {
        "generated_by": "loregraph-loader",
        "counts": {
            "sources": len(sources),
            "entities": len(entities),
            "assertions": len(assertions),
        },
        "note": "Generated notes are projections. The .loregraph JSONL ledger is authoritative.",
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8", newline="\n")


def _clean_stale_generated(vault: Path, manifest: Path, current: set[Path]) -> None:
    previous: set[Path] = set()
    if manifest.exists():
        for value in manifest.read_text(encoding="utf-8").splitlines():
            candidate = (vault / value).resolve()
            try:
                candidate.relative_to(vault.resolve())
            except ValueError:
                continue
            previous.add(candidate)
    current_resolved = {path.resolve() for path in current}
    for stale in previous - current_resolved:
        if not stale.is_file() or stale.suffix.casefold() != ".md":
            continue
        first_kb = stale.read_text(encoding="utf-8", errors="ignore")[:1024]
        if any(
            marker in first_kb
            for marker in ("generated_by: loregraph-loader", "generated_by: eberron-kg-loader")
        ):
            stale.unlink()
    manifest.write_text(
        "\n".join(sorted(path.resolve().relative_to(vault.resolve()).as_posix() for path in current))
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

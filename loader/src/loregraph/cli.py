from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .audit import audit_store, write_audit_sample
from .config import LoaderConfig
from .easy_neo4j import run_easy_pipeline
from .embeddings import embed_chunks
from .neo4j_sink import load_neo4j
from .obsidian_sink import export_obsidian
from .pipeline import (
    apply_batch,
    estimate_extraction,
    extract_sync,
    ingest_manifest,
    prepare_batch,
)
from .schema import SchemaRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loregraph",
        description="Provenance-first loader for lore, campaigns, rules, and notes",
    )
    parser.add_argument("--config", default="config/project.toml", help="Project TOML path")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Parse and chunk sources from a YAML manifest")
    ingest.add_argument("manifest", help="Path to sources.yaml (legacy books.yaml also works)")

    estimate = sub.add_parser("estimate", help="Estimate extraction volume within configured caps")
    estimate.add_argument("--sync", action="store_true", help="Use synchronous rather than Batch rates")

    sub.add_parser("extract", help="Run synchronous OpenAI extraction for the configured budget")
    sub.add_parser("embed", help="Create local or hosted chunk embeddings")

    prepare = sub.add_parser("prepare-batch", help="Create OpenAI Batch request JSONL")
    prepare.add_argument("output", help="Output request JSONL path")

    apply = sub.add_parser("apply-batch", help="Apply downloaded OpenAI Batch result JSONL")
    apply.add_argument("results", help="Batch result JSONL path")

    sub.add_parser("load-neo4j", help="Project the canonical ledger into Neo4j/Aura")
    sub.add_parser("export-obsidian", help="Project the canonical ledger into an Obsidian vault")
    sub.add_parser("audit", help="Validate provenance and referential integrity")

    sample = sub.add_parser("audit-sample", help="Write a human review CSV sample")
    sample.add_argument("output", help="Output CSV path")
    sample.add_argument("--size", type=int, default=25)
    sample.add_argument("--seed", type=int, default=42)

    doctor = sub.add_parser("doctor", help="Check optional dependencies and safe credential presence")
    doctor.add_argument("--json", action="store_true")

    easy = sub.add_parser("easy-neo4j", help="Run pilot-only Neo4j SimpleKGPipeline")
    easy.add_argument("text", help="Pre-parsed Markdown/text file")
    easy.add_argument(
        "--profile",
        choices=["lore", "lore_eberron", "rules", "adventure", "campaign", "character", "notes"],
        default="lore",
    )

    schema = sub.add_parser("export-simple-schema", help="Export a SimpleKGPipeline schema JSON")
    schema.add_argument("output")
    schema.add_argument(
        "--profile",
        choices=["lore", "lore_eberron", "rules", "adventure", "campaign", "character", "notes"],
        default="lore",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = LoaderConfig.from_toml(args.config)
        if args.command == "ingest":
            _print(asdict(ingest_manifest(config, args.manifest)))
        elif args.command == "estimate":
            _print(estimate_extraction(config, batch=not args.sync).to_dict())
        elif args.command == "extract":
            summary, estimate = extract_sync(config)
            _print({"summary": asdict(summary), "estimate": estimate.to_dict()})
        elif args.command == "embed":
            _print(asdict(embed_chunks(config)))
        elif args.command == "prepare-batch":
            _print(prepare_batch(config, args.output).to_dict())
        elif args.command == "apply-batch":
            _print(asdict(apply_batch(config, args.results)))
        elif args.command == "load-neo4j":
            _print(load_neo4j(config))
        elif args.command == "export-obsidian":
            _print(export_obsidian(config))
        elif args.command == "audit":
            report = audit_store(config)
            _print(report.to_dict())
            return 0 if report.ok else 2
        elif args.command == "audit-sample":
            _print({"sampled": write_audit_sample(config, args.output, args.size, args.seed)})
        elif args.command == "doctor":
            _print(_doctor(config))
        elif args.command == "easy-neo4j":
            run_easy_pipeline(config, args.text, args.profile)
            _print({"status": "completed", "mode": "pilot-only"})
        elif args.command == "export-simple-schema":
            registry = SchemaRegistry(config.schema_path)
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(registry.neo4j_simple_schema(args.profile), indent=2),
                encoding="utf-8",
            )
            _print({"output": str(output), "profile": args.profile})
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _doctor(config: LoaderConfig) -> dict[str, Any]:
    modules = ["fitz", "pydantic", "yaml", "docling", "openai", "neo4j", "neo4j_graphrag"]
    return {
        "config": "ok",
        "schema": "ok" if config.schema_path.exists() else "missing",
        "backbone": "ok" if config.backbone_path.exists() else "missing",
        "modules": {name: importlib.util.find_spec(name) is not None for name in modules},
        "credentials_present": {
            "openai": bool(os.getenv("OPENAI_API_KEY")),
            "neo4j_uri": bool(os.getenv("NEO4J_URI")),
            "neo4j_password": bool(os.getenv("NEO4J_PASSWORD")),
        },
    }


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    raise SystemExit(main())

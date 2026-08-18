from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .config import LoaderConfig
from .schema import SchemaRegistry


def run_easy_pipeline(
    config: LoaderConfig,
    text_path: str | Path,
    profile: str = "lore",
) -> None:
    """Pilot-only path using Neo4j's experimental SimpleKGPipeline.

    This is intentionally separate from the evidence-ledger pipeline because the
    SimpleKGPipeline's native graph is useful for rapid exploration but does not
    implement the full Assertion, rights, campaign actuality, or source-version
    semantics required by the canonical model.
    """

    try:
        from neo4j import GraphDatabase
        from neo4j_graphrag.embeddings.openai import OpenAIEmbeddings
        from neo4j_graphrag.experimental.pipeline.kg_builder import SimpleKGPipeline
        from neo4j_graphrag.llm import OpenAILLM
    except ImportError as exc:
        raise RuntimeError("Install the easy extra: pip install -e '.[easy]'") from exc

    credentials = config.neo4j_credentials
    model = config.extraction.model
    embedding_model = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    text = Path(text_path).read_text(encoding="utf-8")
    schema = SchemaRegistry(config.schema_path).neo4j_simple_schema(profile)

    async def run() -> None:
        with GraphDatabase.driver(
            credentials["uri"], auth=(credentials["username"], credentials["password"])
        ) as driver:
            pipeline = SimpleKGPipeline(
                llm=OpenAILLM(model_name=model, model_params={"max_tokens": 1200}),
                driver=driver,
                embedder=OpenAIEmbeddings(model=embedding_model),
                from_file=False,
                schema=schema,
                perform_entity_resolution=True,
                neo4j_database=credentials["database"],
                on_error="RAISE",
            )
            await pipeline.run_async(text=text)

    asyncio.run(run())


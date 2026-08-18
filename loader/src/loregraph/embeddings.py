from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import LoaderConfig
from .ids import stable_id
from .models import Chunk, EmbeddingRecord
from .store import RecordStore


@dataclass(slots=True)
class EmbeddingSummary:
    provider: str
    model: str
    embedded: int
    skipped_current: int
    skipped_permission: int
    input_tokens_estimated: int
    dimensions: int


def embed_chunks(config: LoaderConfig) -> EmbeddingSummary:
    provider = config.embeddings.provider
    if provider == "none":
        raise ValueError("Set embeddings.provider before running embed")
    store = RecordStore(config.workspace_dir)
    chunks = [Chunk(**row) for row in store.read("chunks")]
    selected: list[Chunk] = []
    skipped_current = 0
    skipped_permission = 0
    total_tokens = 0
    for chunk in chunks:
        if chunk.indexing_mode in {"NO_INDEX", "METADATA_ONLY"}:
            continue
        if provider == "openai" and not chunk.remote_processing_allowed:
            skipped_permission += 1
            continue
        fingerprint = _fingerprint(chunk, provider, config.embeddings.model)
        if store.stage_is_current(chunk.id, "embed", fingerprint):
            skipped_current += 1
            continue
        if total_tokens + chunk.token_count > config.embeddings.max_input_tokens:
            continue
        selected.append(chunk)
        total_tokens += chunk.token_count

    vectors = _encode(config, [chunk.text for chunk in selected])
    if len(vectors) != len(selected):
        raise ValueError("Embedding provider returned a different number of vectors than inputs")
    dimensions = len(vectors[0]) if vectors else config.embeddings.dimensions
    if config.embeddings.dimensions and dimensions != config.embeddings.dimensions:
        raise ValueError(
            f"Embedding dimension mismatch: configured {config.embeddings.dimensions}, got {dimensions}"
        )

    existing = store.by_id("embeddings")
    current_hashes = {chunk.id: chunk.text_hash for chunk in chunks}
    for row in existing.values():
        row["active"] = bool(
            row.get("provider") == provider
            and row.get("model") == config.embeddings.model
            and current_hashes.get(row.get("chunk_id")) == row.get("input_hash")
        )
    records: list[EmbeddingRecord] = []
    for chunk, vector in zip(selected, vectors, strict=True):
        record = EmbeddingRecord(
            id=stable_id(
                "global", "embedding", chunk.id, provider, config.embeddings.model, chunk.text_hash
            ),
            chunk_id=chunk.id,
            provider=provider,
            model=config.embeddings.model,
            dimensions=len(vector),
            input_hash=chunk.text_hash,
            vector=[float(value) for value in vector],
            active=True,
        )
        records.append(record)
        existing[record.id] = record.to_dict()
        store.mark_stage(
            chunk.id,
            "embed",
            _fingerprint(chunk, provider, config.embeddings.model),
            "SUCCEEDED",
            {"provider": provider, "model": config.embeddings.model, "dimensions": len(vector)},
        )
    store.replace("embeddings", existing.values())
    return EmbeddingSummary(
        provider=provider,
        model=config.embeddings.model,
        embedded=len(records),
        skipped_current=skipped_current,
        skipped_permission=skipped_permission,
        input_tokens_estimated=total_tokens,
        dimensions=dimensions,
    )


def _encode(config: LoaderConfig, texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    if config.embeddings.provider == "sentence_transformers":
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "Install local embeddings: pip install -e '.[local-embeddings]'"
            ) from exc
        model = SentenceTransformer(config.embeddings.model)
        vectors = model.encode(
            texts,
            batch_size=config.embeddings.batch_size,
            normalize_embeddings=True,
            show_progress_bar=True,
        )
        return [vector.tolist() for vector in vectors]
    if config.embeddings.provider == "openai":
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install the OpenAI extra: pip install -e '.[openai]'") from exc
        client = OpenAI()
        output: list[list[float]] = []
        for start in range(0, len(texts), config.embeddings.batch_size):
            batch = texts[start : start + config.embeddings.batch_size]
            kwargs: dict[str, Any] = {"model": config.embeddings.model, "input": batch}
            if config.embeddings.dimensions:
                kwargs["dimensions"] = config.embeddings.dimensions
            response = client.embeddings.create(**kwargs)
            output.extend(item.embedding for item in sorted(response.data, key=lambda item: item.index))
        return output
    raise ValueError(f"Unknown embedding provider {config.embeddings.provider}")


def _fingerprint(chunk: Chunk, provider: str, model: str) -> str:
    return stable_id("global", "embedding-fingerprint", chunk.text_hash, provider, model)

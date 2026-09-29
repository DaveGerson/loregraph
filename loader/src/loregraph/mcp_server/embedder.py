"""Query embedders for vector search.

A query vector is only comparable with chunk vectors made by the same model,
so the embedder reports its model name and the retrieval layer checks it
against the ``embeddingModel`` recorded on indexed chunks.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol

from .settings import EmbeddingSettings


class QueryEmbedder(Protocol):
    provider: str
    model: str

    def embed(self, text: str) -> list[float]: ...


class SentenceTransformerEmbedder:
    provider = "sentence_transformers"

    def __init__(self, model: str):
        self.model = model
        self._encoder: Any = None
        self._lock = threading.Lock()

    def embed(self, text: str) -> list[float]:
        with self._lock:
            if self._encoder is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise RuntimeError(
                        "Vector search needs local embeddings: pip install -e '.[local-embeddings]'"
                    ) from exc
                self._encoder = SentenceTransformer(self.model)
        # Must match the loader, which stores normalized vectors.
        vector = self._encoder.encode([text], normalize_embeddings=True, show_progress_bar=False)[0]
        return [float(value) for value in vector]


class OpenAIEmbedder:
    provider = "openai"

    def __init__(self, model: str, dimensions: int = 0):
        self.model = model
        self.dimensions = dimensions
        self._client: Any = None

    def embed(self, text: str) -> list[float]:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("Install the OpenAI extra: pip install -e '.[openai]'") from exc
            self._client = OpenAI()
        kwargs: dict[str, Any] = {"model": self.model, "input": [text]}
        if self.dimensions:
            kwargs["dimensions"] = self.dimensions
        response = self._client.embeddings.create(**kwargs)
        return [float(value) for value in response.data[0].embedding]


def build_embedder(settings: EmbeddingSettings) -> QueryEmbedder | None:
    provider = settings.effective_provider
    if provider == "none" or not settings.model:
        return None
    if provider == "sentence_transformers":
        return SentenceTransformerEmbedder(settings.model)
    if provider == "openai":
        return OpenAIEmbedder(settings.model, settings.dimensions)
    raise ValueError(f"Unknown query embedding provider {provider!r}")

from __future__ import annotations

from dataclasses import asdict, dataclass

from .backbone import Backbone
from .config import ExtractionConfig
from .llm import prompt_token_estimate
from .models import Chunk
from .schema import SchemaRegistry


@dataclass(slots=True)
class CostEstimate:
    chunks: int
    eligible_chunks: int
    skipped_remote_permission: int
    skipped_budget_or_cap: int
    input_tokens: int
    maximum_output_tokens: int
    estimated_cost_usd: float | None
    pricing_known: bool
    batch: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def select_within_budget(
    chunks: list[Chunk],
    schema: SchemaRegistry,
    backbone: Backbone,
    config: ExtractionConfig,
    batch: bool,
) -> tuple[list[Chunk], CostEstimate]:
    eligible = [
        chunk
        for chunk in chunks
        if chunk.extraction_eligible and chunk.content_type in config.content_types
    ]
    permitted = [chunk for chunk in eligible if chunk.remote_processing_allowed]
    candidates = sorted(
        permitted,
        key=_priority,
        reverse=True,
    )
    selected: list[Chunk] = []
    input_tokens = 0
    output_tokens = 0
    for chunk in candidates:
        if len(selected) >= config.max_chunks:
            break
        chunk_input = prompt_token_estimate(chunk, schema, backbone)
        if input_tokens + chunk_input > config.max_input_tokens:
            continue
        candidate_output = config.max_output_tokens_per_chunk
        projected_cost = _cost(
            input_tokens + chunk_input,
            output_tokens + candidate_output,
            config,
            batch,
        )
        if projected_cost is not None and projected_cost > config.max_estimated_cost_usd:
            continue
        selected.append(chunk)
        input_tokens += chunk_input
        output_tokens += candidate_output
    cost = _cost(input_tokens, output_tokens, config, batch)
    estimate = CostEstimate(
        chunks=len(selected),
        eligible_chunks=len(eligible),
        skipped_remote_permission=len(eligible) - len(permitted),
        skipped_budget_or_cap=len(permitted) - len(selected),
        input_tokens=input_tokens,
        maximum_output_tokens=output_tokens,
        estimated_cost_usd=cost,
        pricing_known=cost is not None,
        batch=batch,
    )
    return selected, estimate


def _cost(
    input_tokens: int,
    output_tokens: int,
    config: ExtractionConfig,
    batch: bool,
) -> float | None:
    if config.input_price_per_million_usd <= 0 or config.output_price_per_million_usd <= 0:
        return None
    multiplier = config.batch_price_multiplier if batch else 1.0
    return multiplier * (
        input_tokens / 1_000_000 * config.input_price_per_million_usd
        + output_tokens / 1_000_000 * config.output_price_per_million_usd
    )


def _priority(chunk: Chunk) -> tuple[int, int]:
    content_priority = {
        "lore": 7,
        "adventure": 6,
        "rules": 6,
        "statblock": 5,
        "spell": 5,
        "item": 5,
        "table": 2,
    }
    return content_priority.get(chunk.content_type, 3), chunk.token_count

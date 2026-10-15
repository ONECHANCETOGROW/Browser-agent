"""Cost estimates for local reporting only (USD per 1M tokens). Override in data/brain.json -> "pricing". Local models cost 0.

Defaults are the standard paid-tier prices listed on ai.google.dev/gemini-api/docs/pricing on 2026-10-07 (3.x Flash prices are
promotional until 2026-12-31). They are estimates: verify against the provider before relying on them.
"""
from __future__ import annotations

DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.7-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.6-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00},
}


def estimate_cost(provider_kind: str, model: str, input_tokens: int | None, output_tokens: int | None,
                  overrides: dict | None = None) -> float | None:
    """USD estimate, 0.0 for local runtimes, None when the price or the token counts are unknown."""
    if provider_kind == "local":
        return 0.0
    table = {**DEFAULT_PRICING, **(overrides or {})}
    price = table.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return round((input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000, 6)

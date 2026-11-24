"""List prices for model routes.

USD per million tokens (input, output), by (route, model): what the provider's paid plan
charges. A free plan or a model on this machine is charged nothing; list prices say what
the same tokens would cost, which is what "cost per correct automatic resolution" is built
on. A route that is not listed is unpriced and reported as such. Cost is then labelled
incomplete, never guessed.
"""

from __future__ import annotations

PRICING_VERSION = "2026-11-22"

PRICES: dict[tuple[str, str], tuple[float, float]] = {
    ("groq", "openai/gpt-oss-20b"): (0.075, 0.30),
    ("groq", "openai/gpt-oss-120b"): (0.15, 0.60),
    ("openrouter", "openai/gpt-oss-20b"): (0.03, 0.13),
    ("openrouter", "openai/gpt-oss-120b"): (0.05, 0.25),
}

# Routes with no per-token charge: the offline stand-ins and models run on this machine.
FREE_ROUTES = {"keyword", "scripted", "replay", "ollama"}

# Attempt outcomes where no request reached a model, so there is nothing to price.
NOT_CHARGED = {"budget_exhausted", "rate_limited"}


def list_price_usd(
    route: str, model: str, outcome: str, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """What one attempt costs at list price, or None when it cannot be priced."""
    if outcome in NOT_CHARGED or route in FREE_ROUTES:
        return 0.0
    price = PRICES.get((route, model))
    if price is None or input_tokens is None:
        return None
    return (input_tokens or 0) / 1e6 * price[0] + (output_tokens or 0) / 1e6 * price[1]

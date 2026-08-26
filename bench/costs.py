"""Cache-aware billing model.

Every dollar figure the benchmark reports comes from here.

Three token classes bill at different rates against the same input price:
cache reads at 0.1x, cache writes at 1.25x (5-minute TTL) or 2.0x (1-hour
TTL). Claude Code uses the 1-hour TTL, so collapsing the two write classes
into one understates write cost by 60% -- which is precisely the error that
makes naive "tokens saved" claims meaningless.

`input_tokens` in the API usage record counts only uncached tokens, so the
four classes are additive with no double counting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0

# USD per million tokens, (input, output). Anthropic first-party API rates.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Claude Code appends a context-window marker to the model id, e.g.
# "claude-opus-5[1m]". Strip it for price lookup and surface it separately.
_CONTEXT_MARKER = re.compile(r"\[[^\]]*\]$")


# Claude Code stamps locally-generated messages (no API round-trip) with this
# placeholder. They are free and must not count as turns.
SYNTHETIC_MODELS = frozenset({"<synthetic>"})


def is_synthetic(model: str) -> bool:
    return normalize_model(model)[0] in SYNTHETIC_MODELS


class UnknownModelError(KeyError):
    """Raised for an unpriced model. Never guess -- a wrong rate is silent."""


@dataclass(frozen=True)
class TokenUsage:
    """One turn's tokens, split by how each class bills."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_5m_tokens=self.cache_write_5m_tokens + other.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens + other.cache_write_1h_tokens,
        )

    def __radd__(self, other: object) -> "TokenUsage":
        # sum() seeds with int 0; treat that as the identity.
        if other == 0:
            return self
        return self.__add__(other)  # type: ignore[arg-type]

    @property
    def total_input_tokens(self) -> int:
        """Every token sent to the model, regardless of how it billed."""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )

    @property
    def billed_input_equivalent(self) -> float:
        """Input tokens re-expressed at the full input rate, after multipliers.

        This is the number to compare across benchmark arms. Raw token counts
        are not comparable: 100k cache reads and 100k fresh input differ 10x
        in cost.
        """
        return (
            self.input_tokens
            + self.cache_read_tokens * CACHE_READ_MULTIPLIER
            + self.cache_write_5m_tokens * CACHE_WRITE_5M_MULTIPLIER
            + self.cache_write_1h_tokens * CACHE_WRITE_1H_MULTIPLIER
        )


def normalize_model(model: str) -> tuple[str, bool]:
    """Split a Claude Code model id into (base id, is_long_context)."""
    stripped = _CONTEXT_MARKER.sub("", model or "").strip()
    return stripped, stripped != (model or "").strip()


def price_for(model: str) -> tuple[float, float]:
    """(input, output) USD per MTok. Raises rather than guess a rate."""
    base, _ = normalize_model(model)
    if base in SYNTHETIC_MODELS:
        return (0.0, 0.0)
    try:
        return PRICES_PER_MTOK[base]
    except KeyError:
        raise UnknownModelError(
            f"No price for model {model!r}. Add it to PRICES_PER_MTOK "
            f"rather than let the benchmark bill it at zero."
        ) from None


def cost_usd(usage: TokenUsage, model: str) -> float:
    """Billed cost of one turn (or an aggregate) in USD."""
    input_rate, output_rate = price_for(model)
    return (
        usage.billed_input_equivalent * input_rate
        + usage.output_tokens * output_rate
    ) / 1_000_000


def uncached_cost_usd(usage: TokenUsage, model: str) -> float:
    """What the same tokens would have cost with caching disabled.

    Used to report how much of the win is caching (which every tool gets for
    free) versus the context layer itself.
    """
    input_rate, output_rate = price_for(model)
    return (
        usage.total_input_tokens * input_rate
        + usage.output_tokens * output_rate
    ) / 1_000_000

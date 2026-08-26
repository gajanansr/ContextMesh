"""Parse Claude Code session transcripts into billed-cost records.

Claude Code writes every API round-trip to ~/.claude/projects/<slug>/<id>.jsonl
including the full `usage` block. That is ground truth for what a session
actually cost -- no proxy, no instrumentation, no estimation.

Two traps this module exists to avoid:

1. The same assistant message appears on several lines, each carrying an
   identical copy of `usage`. Summing lines triple-counts. We dedupe by
   requestId, which is 1:1 with an API round-trip.
2. `cache_creation_input_tokens` collapses the 5-minute and 1-hour TTL
   writes, which bill at different rates. The nested `cache_creation` object
   splits them; we read that and fall back only when it is absent.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from bench.costs import (
    TokenUsage,
    cost_usd,
    is_synthetic,
    normalize_model,
    uncached_cost_usd,
)


@dataclass(frozen=True)
class Turn:
    """One API round-trip."""

    request_id: str
    message_id: str
    model: str
    timestamp: str
    is_sidechain: bool
    usage: TokenUsage

    @property
    def is_synthetic(self) -> bool:
        """Locally generated, never sent to the API -- free, and not a turn."""
        return is_synthetic(self.model)


@dataclass
class SessionCost:
    """Everything the benchmark needs from one session."""

    session_id: str
    path: Path
    turns: list[Turn] = field(default_factory=list)
    user_messages: int = 0
    malformed_lines: int = 0
    long_context: bool = False

    @property
    def assistant_turns(self) -> int:
        """API round-trips -- the 'turns to completion' metric."""
        return sum(1 for t in self.turns if not t.is_synthetic)

    @property
    def usage(self) -> TokenUsage:
        return sum((t.usage for t in self.turns), TokenUsage())

    @property
    def models(self) -> set[str]:
        return {t.model for t in self.turns}

    @property
    def cost_usd(self) -> float:
        """Billed cost. Summed per turn because models can differ mid-session."""
        return sum(cost_usd(t.usage, t.model) for t in self.turns)

    @property
    def uncached_cost_usd(self) -> float:
        return sum(uncached_cost_usd(t.usage, t.model) for t in self.turns)

    def summary(self) -> dict:
        u = self.usage
        return {
            "session_id": self.session_id,
            "assistant_turns": self.assistant_turns,
            "synthetic_turns": sum(1 for t in self.turns if t.is_synthetic),
            "user_messages": self.user_messages,
            "models": sorted(self.models),
            "long_context": self.long_context,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens,
            "cache_write_5m_tokens": u.cache_write_5m_tokens,
            "cache_write_1h_tokens": u.cache_write_1h_tokens,
            "total_input_tokens": u.total_input_tokens,
            "billed_input_equivalent": round(u.billed_input_equivalent, 1),
            "cost_usd": round(self.cost_usd, 6),
            "uncached_cost_usd": round(self.uncached_cost_usd, 6),
            "malformed_lines": self.malformed_lines,
        }


def _usage_from_record(raw: dict) -> TokenUsage:
    """Build a TokenUsage, splitting cache writes by TTL where possible."""
    creation = raw.get("cache_creation")
    if isinstance(creation, dict):
        write_5m = int(creation.get("ephemeral_5m_input_tokens") or 0)
        write_1h = int(creation.get("ephemeral_1h_input_tokens") or 0)
    else:
        # Older transcripts only carry the collapsed total. Attribute it to
        # the 5-minute rate -- the cheaper of the two, so an unknown TTL
        # under-reports cost rather than inflating the savings claim.
        write_5m = int(raw.get("cache_creation_input_tokens") or 0)
        write_1h = 0

    return TokenUsage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_read_tokens=int(raw.get("cache_read_input_tokens") or 0),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


def iter_turns(path: Path) -> Iterator[Turn]:
    """Yield one Turn per API round-trip, deduped by requestId."""
    seen: set[str] = set()

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "assistant":
                continue

            message = record.get("message") or {}
            raw_usage = message.get("usage")
            if not isinstance(raw_usage, dict):
                continue

            # requestId is the round-trip identity; message id is the fallback
            # for transcripts written before it was recorded.
            key = record.get("requestId") or message.get("id")
            if not key or key in seen:
                continue
            seen.add(key)

            yield Turn(
                request_id=record.get("requestId") or "",
                message_id=message.get("id") or "",
                model=message.get("model") or "",
                timestamp=record.get("timestamp") or "",
                is_sidechain=bool(record.get("isSidechain")),
                usage=_usage_from_record(raw_usage),
            )


def parse_session(path: str | Path) -> SessionCost:
    """Parse one transcript file into a SessionCost."""
    path = Path(path)
    session = SessionCost(session_id=path.stem, path=path)

    for turn in iter_turns(path):
        session.turns.append(turn)
        _, long_ctx = normalize_model(turn.model)
        session.long_context = session.long_context or long_ctx

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                session.malformed_lines += 1
                continue
            if record.get("type") == "user":
                session.user_messages += 1

    return session


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Score a Claude Code transcript.")
    parser.add_argument("transcript", type=Path, nargs="+")
    args = parser.parse_args(argv)

    for path in args.transcript:
        print(json.dumps(parse_session(path).summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

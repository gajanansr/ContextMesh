"""Tests for the benchmark measurement core.

The two failure modes that matter are silent ones: triple-counting deduped
turns, and billing 1-hour cache writes at the 5-minute rate. Both produce
plausible numbers, so both are tested directly.
"""

import json

import pytest

from bench.costs import (
    TokenUsage,
    UnknownModelError,
    cost_usd,
    normalize_model,
    price_for,
    uncached_cost_usd,
)
from bench.transcript import parse_session

USAGE = TokenUsage(
    input_tokens=100,
    output_tokens=200,
    cache_read_tokens=1000,
    cache_write_5m_tokens=400,
    cache_write_1h_tokens=600,
)


def test_billed_input_equivalent_applies_each_multiplier():
    # 100 + 1000*0.1 + 400*1.25 + 600*2.0
    assert USAGE.billed_input_equivalent == pytest.approx(1900.0)


def test_total_input_tokens_ignores_multipliers():
    assert USAGE.total_input_tokens == 2100


def test_cost_uses_opus_rates():
    assert cost_usd(USAGE, "claude-opus-5") == pytest.approx(0.0145)


def test_uncached_cost_is_higher_than_billed():
    assert uncached_cost_usd(USAGE, "claude-opus-5") == pytest.approx(0.0155)


def test_one_hour_writes_cost_more_than_five_minute_writes():
    """The whole point of splitting TTLs -- 2.0x vs 1.25x."""
    five_min = TokenUsage(cache_write_5m_tokens=1000)
    one_hour = TokenUsage(cache_write_1h_tokens=1000)
    assert cost_usd(one_hour, "claude-opus-5") == pytest.approx(
        cost_usd(five_min, "claude-opus-5") * 1.6
    )


def test_usage_is_summable():
    assert sum([USAGE, USAGE], TokenUsage()).input_tokens == 200


def test_normalize_strips_context_window_marker():
    assert normalize_model("claude-opus-5[1m]") == ("claude-opus-5", True)
    assert normalize_model("claude-opus-5") == ("claude-opus-5", False)


def test_long_context_model_id_still_prices():
    assert price_for("claude-opus-5[1m]") == (5.00, 25.00)


def test_unknown_model_raises_rather_than_billing_zero():
    with pytest.raises(UnknownModelError):
        cost_usd(USAGE, "gpt-4")


def _line(request_id, msg_id, **usage):
    return json.dumps(
        {
            "type": "assistant",
            "requestId": request_id,
            "timestamp": "2026-08-27T00:00:00Z",
            "message": {"id": msg_id, "model": "claude-opus-5", "usage": usage},
        }
    )


def test_repeated_lines_for_one_request_are_counted_once(tmp_path):
    """Claude Code writes several identical usage lines per round-trip."""
    usage = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30}
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([_line("req_1", "msg_1", **usage)] * 3))

    session = parse_session(path)

    assert session.assistant_turns == 1
    assert session.usage.input_tokens == 10
    assert session.usage.cache_read_tokens == 30


def test_distinct_requests_accumulate(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        "\n".join(
            [
                _line("req_1", "msg_1", input_tokens=10, output_tokens=1),
                _line("req_2", "msg_2", input_tokens=5, output_tokens=2),
            ]
        )
    )
    session = parse_session(path)
    assert session.assistant_turns == 2
    assert session.usage.input_tokens == 15


def test_cache_creation_ttls_are_split(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        _line(
            "req_1",
            "msg_1",
            cache_creation={
                "ephemeral_5m_input_tokens": 7,
                "ephemeral_1h_input_tokens": 11,
            },
            cache_creation_input_tokens=18,
        )
    )
    usage = parse_session(path).usage
    assert usage.cache_write_5m_tokens == 7
    assert usage.cache_write_1h_tokens == 11


def test_collapsed_cache_creation_falls_back_to_cheaper_rate(tmp_path):
    """Unknown TTL must under-report cost, never inflate savings."""
    path = tmp_path / "s.jsonl"
    path.write_text(_line("req_1", "msg_1", cache_creation_input_tokens=18))
    usage = parse_session(path).usage
    assert usage.cache_write_5m_tokens == 18
    assert usage.cache_write_1h_tokens == 0


def test_non_assistant_and_malformed_lines_are_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        "\n".join(
            [
                "{not json",
                json.dumps({"type": "user", "message": {}}),
                _line("req_1", "msg_1", input_tokens=4),
            ]
        )
    )
    session = parse_session(path)
    assert session.assistant_turns == 1
    assert session.user_messages == 1
    assert session.malformed_lines == 1


def test_synthetic_model_is_free_and_not_a_turn(tmp_path):
    """Locally-generated messages never hit the API."""
    path = tmp_path / "s.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "requestId": "req_1",
                        "message": {
                            "id": "m1",
                            "model": "<synthetic>",
                            "usage": {"input_tokens": 99, "output_tokens": 99},
                        },
                    }
                ),
                _line("req_2", "msg_2", input_tokens=10, output_tokens=1),
            ]
        )
    )
    session = parse_session(path)
    assert session.assistant_turns == 1
    assert session.summary()["synthetic_turns"] == 1
    assert cost_usd(USAGE, "<synthetic>") == 0.0

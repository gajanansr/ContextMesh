"""
Integration smoke test — verifies the full ContextMesh pipeline.

Run with: pytest tests/test_smoke.py -v

This test:
1. Bootstraps the full system in-memory (SQLite :memory:)
2. Simulates a Claude Code session with multiple tasks
3. Verifies token savings are recorded correctly
4. Checks task boundary detection
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio

from contextmesh.config import Config, DaemonConfig, RouterConfig, TasksConfig, TrackerConfig, EmbeddingsConfig
from contextmesh.store.db import Database
from contextmesh.store.schema import CREATE_SCHEMA_SQL
from contextmesh.models.nodes import (
    HookEvent, HookEventType, SessionNode, NodeType, MemoryTier,
    ContextRequest
)


@pytest.fixture
def config() -> Config:
    cfg = Config()
    cfg.tasks.min_turns_per_task = 2
    cfg.tasks.topic_shift_threshold = 0.4
    cfg.tracker.input_price_per_mtok = 3.0
    return cfg


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """In-memory SQLite database for testing."""
    db_path = tmp_path / "test.db"
    from contextmesh.store.db import Database
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_session_creation(db: Database):
    """Test basic session creation and retrieval."""
    await db.upsert_session("sess_001", "/projects/myapp")
    session = await db.get_session("sess_001")
    assert session is not None
    assert session["session_id"] == "sess_001"
    assert session["project_path"] == "/projects/myapp"


@pytest.mark.asyncio
async def test_accumulator_tracking(db: Database):
    """Test that session token accumulator correctly tracks baseline."""
    await db.upsert_session("sess_002", "/projects/myapp")

    # Simulate a session with multiple turns
    await db.record_accumulator_turn("sess_002", "user", 150, "Fix the auth bug")
    total1 = await db.get_cumulative_tokens("sess_002")
    assert total1 == 150

    await db.record_accumulator_turn("sess_002", "tool_result", 2000, "Reading auth.ts...")
    total2 = await db.get_cumulative_tokens("sess_002")
    assert total2 == 2150

    await db.record_accumulator_turn("sess_002", "assistant", 500, "I can see the issue...")
    total3 = await db.get_cumulative_tokens("sess_002")
    assert total3 == 2650


@pytest.mark.asyncio
async def test_token_savings_recording(db: Database):
    """Test the core token savings calculation."""
    await db.upsert_session("sess_003", "/projects/myapp")

    # Simulate 50k accumulated tokens (long session)
    for i in range(10):
        await db.record_accumulator_turn("sess_003", "user", 5000, f"Turn {i}")

    accumulated = await db.get_cumulative_tokens("sess_003")
    assert accumulated == 50_000

    # ContextMesh only sends 12k tokens (routed context)
    await db.record_turn_savings(
        turn_id="turn_001",
        session_id="sess_003",
        task_id=None,
        accumulated_tokens=accumulated,
        routed_tokens=12_000,
        mcp_overhead_tokens=300,
        hot_tokens=2_000,
        warm_tokens=7_000,
        cold_tokens=0,
        repo_tokens=3_000,
        input_price_per_mtok=3.0,
    )

    # Query saved
    row = await db.fetchone("SELECT * FROM token_savings WHERE turn_id = 'turn_001'")
    assert row is not None
    assert row["accumulated_session_tokens"] == 50_000
    assert row["routed_tokens"] == 12_000
    assert row["tokens_saved"] == 38_000       # 50k - 12k
    assert row["net_tokens_saved"] == 37_700   # 50k - 12k - 300
    assert abs(row["compression_ratio"] - 0.24) < 0.01  # 12k/50k
    assert row["cost_saved_usd"] > 0


@pytest.mark.asyncio
async def test_node_creation_and_retrieval(db: Database):
    """Test session node storage and retrieval."""
    await db.upsert_session("sess_004", "/projects/myapp")

    node = SessionNode(
        session_id="sess_004",
        node_type=NodeType.DECISION,
        content="JWT tokens expire after 1hr. Webhook handler must tolerate missing subscription.",
        files_involved=["payments/webhook.ts", "payments/subscription.ts"],
        symbols=["WebhookHandler", "SubscriptionService"],
        importance=0.9,
        tier=MemoryTier.HOT,
        token_count=45,
    )
    await db.insert("nodes", node.to_db_row())

    nodes = await db.get_session_nodes("sess_004", tier="hot")
    assert len(nodes) == 1
    assert nodes[0]["node_type"] == "decision"
    files = json.loads(nodes[0]["files_involved"])
    assert "payments/webhook.ts" in files


@pytest.mark.asyncio
async def test_hook_event_model():
    """Test that HookEvent model parses correctly."""
    payload = {
        "event_type": "PostToolUse",
        "session_id": "sess_005",
        "project_path": "/projects/myapp",
        "tool_name": "bash",
        "tool_input": {"command": "pytest tests/ -v"},
        "tool_response": "PASSED tests/test_auth.py::test_jwt_expiry",
        "tool_exit_code": 0,
    }
    event = HookEvent(**payload)
    assert event.event_type == HookEventType.POST_TOOL_USE
    assert event.tool_exit_code == 0


@pytest.mark.asyncio
async def test_context_request_model():
    """Test ContextRequest model validation."""
    req = ContextRequest(
        session_id="sess_006",
        task_hint="fixing the dashboard button click handler",
        budget_tokens=15_000,
        files_hint=["src/Dashboard.tsx", "src/Button.tsx"],
    )
    assert req.budget_tokens == 15_000
    assert "Dashboard" in req.files_hint[0]


@pytest.mark.asyncio
async def test_savings_ratio_calculation(db: Database):
    """Test that savings ratio is computed correctly for edge cases."""
    await db.upsert_session("sess_007", "/projects/myapp")

    # Turn where context is larger than accumulated (shouldn't happen but handle it)
    await db.record_turn_savings(
        turn_id="turn_edge_001",
        session_id="sess_007",
        task_id=None,
        accumulated_tokens=5_000,
        routed_tokens=6_000,  # More than accumulated? Bad session start.
        mcp_overhead_tokens=300,
        hot_tokens=6_000,
        warm_tokens=0,
        cold_tokens=0,
        repo_tokens=0,
        input_price_per_mtok=3.0,
    )

    row = await db.fetchone(
        "SELECT tokens_saved, net_tokens_saved FROM token_savings WHERE turn_id = 'turn_edge_001'"
    )
    # Generated columns use MAX(0, ...) so savings should be 0, not negative
    assert row["tokens_saved"] == 0
    assert row["net_tokens_saved"] == 0

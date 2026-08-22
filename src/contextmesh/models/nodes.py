"""Node and Edge type definitions for the ContextMesh dual-graph system."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "n") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ──────────────────────────────────────────────────────────
# Node Types
# ──────────────────────────────────────────────────────────

class NodeType(str, Enum):
    # ── Session / interaction events ──
    USER_PROMPT = "user_prompt"
    ASSISTANT_RESPONSE = "assistant_response"

    # ── Task structure ──
    TASK = "task"
    SUBTASK = "subtask"

    # ── Knowledge nodes ──
    DECISION = "decision"           # A architectural or implementation decision
    FACT = "fact"                   # A discovered fact about the codebase
    HYPOTHESIS = "hypothesis"       # An unconfirmed theory being tested
    UNRESOLVED_ISSUE = "unresolved_issue"  # Known problem not yet fixed

    # ── Work events ──
    BUG = "bug"                     # A bug found
    SOLUTION = "solution"           # A working fix/solution
    ERROR = "error"                 # An error encountered (runtime, compile, etc.)
    TEST_RESULT = "test_result"     # Test run output

    # ── File events ──
    FILE_READ = "file_read"
    FILE_MODIFICATION = "file_modification"

    # ── Tool events ──
    TOOL_RESULT = "tool_result"     # Raw tool result (bash, search, etc.)

    # ── Architecture ──
    ARCHITECTURE_CHANGE = "architecture_change"
    DISCOVERED_DEPENDENCY = "discovered_dependency"

    # ── VCS ──
    COMMIT = "commit"

    # ── Summaries (generated async) ──
    THREAD_SUMMARY = "thread_summary"
    FEATURE_SUMMARY = "feature_summary"
    PROJECT_SUMMARY = "project_summary"

    # ── Repo graph nodes ──
    REPO_FILE = "repo_file"
    REPO_FUNCTION = "repo_function"
    REPO_CLASS = "repo_class"
    REPO_METHOD = "repo_method"
    REPO_MODULE = "repo_module"


class MemoryTier(str, Enum):
    HOT = "hot"      # Current task, actively injected
    WARM = "warm"    # Related tasks, retrieved on demand
    COLD = "cold"    # Historical archive, never auto-injected


class TaskType(str, Enum):
    PROJECT = "project"
    FEATURE = "feature"
    THREAD = "thread"


class TaskStatus(str, Enum):
    ACTIVE = "active"
    DORMANT = "dormant"
    COMPLETED = "completed"


# ──────────────────────────────────────────────────────────
# Session Node Model
# ──────────────────────────────────────────────────────────

class SessionNode(BaseModel):
    """A node in the session graph."""
    node_id: str = Field(default_factory=lambda: _new_id("n"))
    session_id: str
    task_id: str | None = None
    node_type: NodeType
    content: str                        # Raw content
    summary: str | None = None          # Abstracted (filled async)
    files_involved: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    git_commit: str | None = None
    confidence: float = 1.0
    importance: float = 0.5             # 0.0–1.0
    tier: MemoryTier = MemoryTier.HOT
    token_count: int = 0
    created_at: str = Field(default_factory=_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_db_row(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "node_type": self.node_type.value,
            "content": self.content,
            "summary": self.summary,
            "files_involved": json.dumps(self.files_involved),
            "symbols": json.dumps(self.symbols),
            "git_commit": self.git_commit,
            "confidence": self.confidence,
            "importance": self.importance,
            "tier": self.tier.value,
            "token_count": self.token_count,
            "created_at": self.created_at,
            "metadata": json.dumps(self.metadata),
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "SessionNode":
        return cls(
            node_id=row["node_id"],
            session_id=row["session_id"],
            task_id=row.get("task_id"),
            node_type=NodeType(row["node_type"]),
            content=row["content"],
            summary=row.get("summary"),
            files_involved=json.loads(row.get("files_involved") or "[]"),
            symbols=json.loads(row.get("symbols") or "[]"),
            git_commit=row.get("git_commit"),
            confidence=row.get("confidence", 1.0),
            importance=row.get("importance", 0.5),
            tier=MemoryTier(row.get("tier", "hot")),
            token_count=row.get("token_count", 0),
            created_at=row["created_at"],
            metadata=json.loads(row.get("metadata") or "{}"),
        )


# ──────────────────────────────────────────────────────────
# Task Model
# ──────────────────────────────────────────────────────────

class Task(BaseModel):
    """A task node in the hierarchical task model."""
    task_id: str = Field(default_factory=lambda: _new_id("t"))
    session_id: str
    parent_task_id: str | None = None
    name: str
    description: str | None = None
    task_type: TaskType = TaskType.THREAD
    status: TaskStatus = TaskStatus.ACTIVE
    tier: MemoryTier = MemoryTier.HOT
    started_at: str = Field(default_factory=_now_iso)
    last_active: str | None = None
    files_involved: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_db_row(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "parent_task_id": self.parent_task_id,
            "name": self.name,
            "description": self.description,
            "task_type": self.task_type.value,
            "status": self.status.value,
            "tier": self.tier.value,
            "started_at": self.started_at,
            "last_active": self.last_active,
            "files_involved": json.dumps(self.files_involved),
            "symbols": json.dumps(self.symbols),
            "metadata": json.dumps(self.metadata),
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "Task":
        return cls(
            task_id=row["task_id"],
            session_id=row["session_id"],
            parent_task_id=row.get("parent_task_id"),
            name=row["name"],
            description=row.get("description"),
            task_type=TaskType(row.get("task_type", "thread")),
            status=TaskStatus(row.get("status", "active")),
            tier=MemoryTier(row.get("tier", "hot")),
            started_at=row["started_at"],
            last_active=row.get("last_active"),
            files_involved=json.loads(row.get("files_involved") or "[]"),
            symbols=json.loads(row.get("symbols") or "[]"),
            metadata=json.loads(row.get("metadata") or "{}"),
        )


# ──────────────────────────────────────────────────────────
# Repo Graph Node Model
# ──────────────────────────────────────────────────────────

class RepoNode(BaseModel):
    """A node in the repository code graph."""
    node_id: str = Field(default_factory=lambda: _new_id("r"))
    project_path: str
    repo_node_type: NodeType  # One of REPO_* types
    name: str
    qualified_name: str | None = None
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    language: str | None = None
    signature: str | None = None
    docstring: str | None = None
    token_count: int = 0
    last_modified: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_db_row(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "project_path": self.project_path,
            "repo_node_type": self.repo_node_type.value,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "language": self.language,
            "signature": self.signature,
            "docstring": self.docstring,
            "token_count": self.token_count,
            "last_modified": self.last_modified,
            "metadata": json.dumps(self.metadata),
        }


# ──────────────────────────────────────────────────────────
# Hook Event Models (from Claude Code)
# ──────────────────────────────────────────────────────────

class HookEventType(str, Enum):
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SUBAGENT_STOP = "SubagentStop"
    NOTIFICATION = "Notification"


class HookEvent(BaseModel):
    """Payload sent by Claude Code hooks to the ContextMesh daemon."""
    event_type: HookEventType
    session_id: str
    project_path: str = ""

    # UserPromptSubmit fields
    prompt: str | None = None

    # Tool use fields
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_response: str | None = None  # PostToolUse only
    tool_exit_code: int | None = None  # PostToolUse bash

    # Stop fields
    stop_reason: str | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextRequest(BaseModel):
    """Request to the Context Router for a curated context projection."""
    session_id: str
    task_hint: str | None = None           # User's current intent/task description
    budget_tokens: int = 15_000
    include_repo_graph: bool = True
    include_decisions: bool = True
    include_unresolved: bool = True
    files_hint: list[str] = Field(default_factory=list)   # Files Claude is working on


class ContextResponse(BaseModel):
    """Curated context projection returned by the Context Router."""
    session_id: str
    task_id: str | None = None
    task_name: str | None = None

    # The assembled context text (ready to inject)
    context_text: str = ""

    # Token accounting
    total_tokens: int = 0
    hot_tokens: int = 0
    warm_tokens: int = 0
    cold_tokens: int = 0
    repo_tokens: int = 0

    # Included node ids (for savings tracking)
    included_node_ids: list[str] = Field(default_factory=list)

    # Savings metadata
    accumulated_session_tokens: int = 0   # Full session baseline
    tokens_saved: int = 0
    compression_ratio: float = 1.0

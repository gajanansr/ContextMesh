"""Edge type definitions for the ContextMesh dual-graph."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _new_edge_id() -> str:
    return f"e_{uuid.uuid4().hex[:16]}"


class EdgeType(str, Enum):
    # ── Task/thread grouping ──
    SAME_TASK = "same_task"
    SAME_THREAD = "same_thread"

    # ── Causal / logical ──
    DEPENDS_ON = "depends_on"
    CAUSED_BY = "caused_by"
    SOLVED_BY = "solved_by"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"

    # ── Temporal ──
    FOLLOWED_BY = "followed_by"

    # ── Referential ──
    REFERENCES = "references"
    RELATED_TO = "related_to"

    # ── Code relationships (also used in repo graph) ──
    CALLS = "calls"
    IMPORTS = "imports"
    SAME_FILE = "same_file"
    TESTED_BY = "tested_by"
    INHERITS = "inherits"
    WRITES_TO = "writes_to"
    READS_FROM = "reads_from"

    # ── Cross-graph (session node ↔ repo node) ──
    TOUCHES_FILE = "touches_file"
    TOUCHES_SYMBOL = "touches_symbol"


# Edge weight semantics:
# Weight represents the strength of the relationship for context routing.
# Higher weight = stronger signal that the connected node is relevant.
DEFAULT_EDGE_WEIGHTS: dict[EdgeType, float] = {
    EdgeType.SAME_TASK: 1.0,
    EdgeType.SAME_THREAD: 0.9,
    EdgeType.CAUSED_BY: 0.95,
    EdgeType.SOLVED_BY: 0.95,
    EdgeType.DEPENDS_ON: 0.85,
    EdgeType.SUPERSEDES: 0.80,
    EdgeType.CONTRADICTS: 0.75,
    EdgeType.CALLS: 0.80,
    EdgeType.IMPORTS: 0.70,
    EdgeType.SAME_FILE: 0.60,
    EdgeType.TESTED_BY: 0.65,
    EdgeType.INHERITS: 0.70,
    EdgeType.WRITES_TO: 0.75,
    EdgeType.READS_FROM: 0.60,
    EdgeType.REFERENCES: 0.55,
    EdgeType.FOLLOWED_BY: 0.40,
    EdgeType.RELATED_TO: 0.35,
    EdgeType.TOUCHES_FILE: 0.50,
    EdgeType.TOUCHES_SYMBOL: 0.55,
}


class SessionEdge(BaseModel):
    """An edge in the session graph."""
    edge_id: str = Field(default_factory=_new_edge_id)
    source_id: str
    target_id: str
    edge_type: EdgeType
    weight: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_db_row(self) -> dict[str, Any]:
        import json
        return {
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type.value,
            "weight": self.weight,
            "metadata": json.dumps(self.metadata),
        }


class RepoEdge(BaseModel):
    """An edge in the repository code graph."""
    edge_id: str = Field(default_factory=_new_edge_id)
    source_id: str
    target_id: str
    edge_type: EdgeType
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_db_row(self) -> dict[str, Any]:
        import json
        return {
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type.value,
            "metadata": json.dumps(self.metadata),
        }

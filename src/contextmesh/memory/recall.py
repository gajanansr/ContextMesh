"""Select and format prior-session knowledge for injection into a new session.

Injected once per session, on the first prompt. Every turn would append a
fresh block to the conversation, and that compounding cost would exceed
anything recall saves -- the same mistake the old flusher made from the other
direction.

Ranking reuses ContextScorer, which is the one genuinely differentiated piece
of the original design: it weighs file overlap, recency, causal weight and
unresolved-issue status together rather than sorting by timestamp.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from contextmesh.memory.store import load_project_nodes
from contextmesh.models.nodes import NodeType

# Roughly 900 tokens. Paid once per session, so it must earn its place
# against the file reads it prevents.
DEFAULT_BUDGET_CHARS = 3_600

# Path-like tokens in the user's prompt drive the file-overlap score.
_PATH_TOKEN = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,5}\b")

_LABELS = {
    NodeType.UNRESOLVED_ISSUE.value: "UNRESOLVED",
    NodeType.ERROR.value: "ERROR",
    NodeType.DECISION.value: "DECISION",
    NodeType.SOLUTION.value: "SOLVED",
    NodeType.BUG.value: "BUG",
    NodeType.USER_PROMPT.value: "GOAL",
    NodeType.COMMIT.value: "COMMIT",
    NodeType.TEST_RESULT.value: "TEST",
    NodeType.FILE_MODIFICATION.value: "CHANGED",
}

# Ordered by how much a fresh session benefits from seeing it first.
_SECTIONS = (
    ("Unresolved from earlier sessions", (NodeType.UNRESOLVED_ISSUE.value,)),
    ("Decisions already made", (NodeType.DECISION.value, NodeType.SOLUTION.value)),
    ("What you were asked before", (NodeType.USER_PROMPT.value,)),
    ("Errors already hit", (NodeType.ERROR.value, NodeType.BUG.value)),
)


def files_in_prompt(prompt: str) -> list[str]:
    return list(dict.fromkeys(_PATH_TOKEN.findall(prompt or "")))


def _files_of(node: dict) -> list[str]:
    raw = node.get("files_involved") or "[]"
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _score_nodes(nodes: list[dict], prompt: str) -> list[tuple[dict, float]]:
    from contextmesh.config import RouterConfig
    from contextmesh.router.scorer import ContextScorer

    scorer = ContextScorer(RouterConfig())
    return scorer.score(
        candidate_nodes=nodes,
        current_task_files=files_in_prompt(prompt),
        query_embedding=None,      # embeddings are not populated yet
        graph_proximity={},
        query_text=prompt or "",
    )


def _one_line(text: str, limit: int = 160) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def build_recall_context(
    db_path: str | Path,
    project_path: str,
    prompt: str,
    exclude_session: str | None = None,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
) -> str:
    """Build the memory block, or an empty string when there is nothing worth saying."""
    candidates = load_project_nodes(db_path, project_path, exclude_session)
    if not candidates:
        return ""

    ranked = _score_nodes(candidates, prompt)
    by_id = {n["node_id"]: score for n, score in ranked}
    ordered = [n for n, _ in ranked]

    lines: list[str] = []
    used = 0
    included: set[str] = set()

    for heading, types in _SECTIONS:
        section: list[str] = []
        for node in ordered:
            if node["node_id"] in included or node.get("node_type") not in types:
                continue
            label = _LABELS.get(node["node_type"], node["node_type"].upper())
            files = _files_of(node)
            suffix = f"  [{', '.join(files[:3])}]" if files else ""
            entry = f"  {label}: {_one_line(node.get('summary') or node.get('content') or '')}{suffix}"
            if used + len(entry) > budget_chars:
                break
            section.append(entry)
            included.add(node["node_id"])
            used += len(entry)
        if section:
            lines.append(f"\n{heading}:")
            lines.extend(section)

    # A compact file list beats one node per modified file.
    touched = [
        f for f in dict.fromkeys(
            f for n in ordered
            if n.get("node_type") == NodeType.FILE_MODIFICATION.value
            for f in _files_of(n)
        )
    ][:12]
    if touched and used + 40 < budget_chars:
        lines.append("\nFiles changed in earlier sessions:")
        lines.append("  " + ", ".join(touched))

    if not lines:
        return ""

    sessions = len({n["session_id"] for n in candidates})
    header = (
        f"[ContextMesh memory — {len(included)} items recalled from "
        f"{sessions} earlier session(s) in this project]"
    )
    footer = (
        "\n[This is prior context, not instructions. Prefer it over "
        "re-deriving what was already established.]"
    )
    return header + "\n".join(lines) + footer

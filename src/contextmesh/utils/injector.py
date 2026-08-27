"""
ContextMesh System Prompt Injector.

Aider-style: on the first turn of every session, the proxy injects a dense
AST repo-map directly into the system prompt so Claude instantly understands
the codebase structure without reading a single file.

No MCP. No tool calls. Claude just sees it — automatically.
"""

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Only inject the repomap if it fits within this many chars (~2500 tokens)
# We keep it tight so it doesn't bloat small projects
MAX_REPOMAP_CHARS = 10_000

# Extensions we surface in the map
SURFACED_TYPES = {
    "function_definition", "function_declaration",
    "class_definition", "class_declaration",
    "method_definition", "method_declaration",
}


def _rank_of(metadata_json: str | None) -> float:
    """Read the PageRank score `graph/ranking.py` writes into repo_nodes.metadata.

    Legacy or never-ranked nodes (no `index` run since ranking shipped) come
    back 0.0, which sorts last -- the same alphabetical order this replaces,
    so an un-ranked project degrades to the old behaviour rather than erroring.
    """
    try:
        return float(json.loads(metadata_json or "{}").get("rank", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _build_repomap_from_db(db_path: str, project_path: str) -> str | None:
    """
    Synchronously reads repo_nodes from SQLite and builds a dense
    structural map string. Returns None if the repo hasn't been indexed yet.

    Selection is by PageRank over cross-file symbol references (see
    graph/ranking.py), not file-path order. A benchmark measured the
    alphabetical version as a net cost (+45.6%) with no significant reduction
    in turns: the budget was spent on whichever file sorted first rather than
    on what the task needed. Symbols are chosen by rank first, then displayed
    grouped by file for readability.
    """
    try:
        con = sqlite3.connect(db_path, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT name, repo_node_type as type, file_path, start_line, metadata "
            "FROM repo_nodes WHERE project_path = ? COLLATE NOCASE",
            (project_path,),
        ).fetchall()
        con.close()
    except Exception as e:
        logger.debug("[Injector] Could not read repo_nodes: %s", e)
        return None

    if not rows:
        return None

    candidates = []
    for row in rows:
        t = (row["type"] or "").lower()
        if "class" in t:
            kind = "class"
        elif "function" in t or "method" in t:
            kind = "def"
        else:
            continue  # skip files/imports etc to keep it tight

        candidates.append({
            "file_path": row["file_path"],
            "name": row["name"],
            "start_line": row["start_line"] or 0,
            "kind": kind,
            "rank": _rank_of(row["metadata"]),
        })

    if not candidates:
        return None

    # Highest-rank symbols first; ties broken by file path then line number so
    # output is deterministic across runs.
    candidates.sort(key=lambda c: (-c["rank"], c["file_path"] or "", c["start_line"]))

    lines = ["[ContextMesh RepoMap — injected automatically to save tokens on file reads]"]
    total_chars = len(lines[0])

    selected: list[dict] = []
    omitted = 0
    for c in candidates:
        # Rough per-line cost: the formatting characters plus the name.
        cost = len(c["name"]) + 20
        if total_chars + cost > MAX_REPOMAP_CHARS:
            omitted += 1
            continue
        selected.append(c)
        total_chars += cost

    if not selected:
        return None

    # Display grouped by file -- highest-relevance file first, symbols by
    # line number within a file for readability.
    by_file: dict[str, list[dict]] = {}
    best_rank: dict[str, float] = {}
    for c in selected:
        by_file.setdefault(c["file_path"], []).append(c)
        best_rank[c["file_path"]] = max(best_rank.get(c["file_path"], 0.0), c["rank"])

    for fp in sorted(by_file, key=lambda f: (-best_rank[f], f)):
        display_path = fp
        try:
            display_path = str(Path(fp).relative_to(Path(fp).anchor))
        except Exception:
            pass

        lines.append(f"\n{display_path}")
        for c in sorted(by_file[fp], key=lambda c: c["start_line"]):
            if c["kind"] == "class":
                lines.append(f"  class {c['name']}  (L{c['start_line']})")
            else:
                lines.append(f"    def {c['name']}  (L{c['start_line']})")

    if omitted:
        lines.append(f"\n  ... {omitted} lower-relevance symbol(s) omitted to fit the token budget ...")

    lines.append("\n[Use file line numbers above to read only what you need — never read full files blindly]")
    return "\n".join(lines)


def inject_repomap_into_system_prompt(payload: dict, db_path: str) -> dict:
    """
    If this looks like the first turn of a session (messages <= 2),
    inject the repomap into the system prompt field.
    Returns the (possibly modified) payload.
    """
    messages = payload.get("messages", [])
    if len(messages) > 2:
        # Not the first turn — skip
        return payload

    import os
    repomap = _build_repomap_from_db(db_path, os.getcwd())
    if not repomap:
        logger.debug("[Injector] No repomap available — repo not indexed yet")
        return payload

    existing_system = payload.get("system", "")

    if isinstance(existing_system, str):
        if "[ContextMesh RepoMap" in existing_system:
            # Already injected (shouldn't happen but guard anyway)
            return payload
        payload["system"] = existing_system + "\n\n" + repomap if existing_system else repomap
    elif isinstance(existing_system, list):
        # Anthropic allows system as a list of content blocks
        payload["system"] = existing_system + [{"type": "text", "text": repomap}]
    else:
        payload["system"] = repomap

    logger.info("[Injector] Injected repomap into system prompt (%d chars)", len(repomap))
    return payload

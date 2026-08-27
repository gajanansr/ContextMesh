"""
ContextMesh System Prompt Injector.

Aider-style: on the first turn of every session, the proxy injects a dense
AST repo-map directly into the system prompt so Claude instantly understands
the codebase structure without reading a single file.

No MCP. No tool calls. Claude just sees it — automatically.
"""

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


def _build_repomap_from_db(db_path: str, project_path: str) -> str | None:
    """
    Synchronously reads repo_nodes from SQLite and builds a dense
    structural map string. Returns None if the repo hasn't been indexed yet.
    """
    try:
        con = sqlite3.connect(db_path, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT name, repo_node_type as type, file_path, start_line FROM repo_nodes WHERE project_path = ? COLLATE NOCASE ORDER BY file_path, start_line", (project_path,)
        ).fetchall()
        con.close()
    except Exception as e:
        logger.debug("[Injector] Could not read repo_nodes: %s", e)
        return None

    if not rows:
        return None

    # Group by file
    files: dict[str, list] = {}
    for row in rows:
        fp = row["file_path"]
        if fp not in files:
            files[fp] = []
        files[fp].append(row)

    lines = ["[ContextMesh RepoMap — injected automatically to save tokens on file reads]"]

    total_chars = 0
    for fp in sorted(files.keys()):
        # Make path relative-looking (strip common prefix)
        display_path = fp
        try:
            display_path = str(Path(fp).relative_to(Path(fp).anchor))
        except Exception:
            pass

        file_line = f"\n{display_path}"
        lines.append(file_line)
        total_chars += len(file_line)

        for node in files[fp]:
            t = node["type"].lower()
            name = node["name"]
            lineno = node["start_line"]

            if "class" in t:
                symbol = f"  class {name}  (L{lineno})"
            elif "function" in t or "method" in t:
                symbol = f"    def {name}  (L{lineno})"
            else:
                continue  # skip imports etc to keep it tight

            lines.append(symbol)
            total_chars += len(symbol)

            if total_chars > MAX_REPOMAP_CHARS:
                lines.append("  ... (truncated — use get_project_architecture() for full map)")
                break
        else:
            continue
        break

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

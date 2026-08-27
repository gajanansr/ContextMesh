"""Synchronous SQLite access for the memory hooks.

The hooks run on the critical path of a keystroke, so this deliberately uses
stdlib sqlite3 rather than the async Database layer -- no event loop, no
driver import, no connection pool to warm.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pydantic is heavy; the recall fast path must not pay for it
    from contextmesh.models.nodes import SessionNode

NODE_COLUMNS = (
    "node_id", "session_id", "task_id", "node_type", "content", "summary",
    "files_involved", "symbols", "git_commit", "confidence", "importance",
    "tier", "token_count", "created_at", "metadata",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_project(project_path: str | Path) -> str:
    """Canonical form of a project path, for exact-match storage and lookup.

    Claude Code reports cwd fully resolved. On macOS /tmp and /var are
    symlinks into /private, so a path stored unresolved never matches the cwd
    the hook is handed, and recall silently returns nothing -- silently,
    because the hook swallows every exception by design. Both sides normalise
    through here so the comparison cannot drift.

    Resolving does not fix case. macOS and Windows are case-insensitive but
    case-preserving, so `~/documents/proj` and `~/Documents/proj` are the same
    directory yet resolve to different strings, splitting one project into two
    memory silos. Lookups therefore compare COLLATE NOCASE rather than
    lowercasing here, which would corrupt paths on case-sensitive filesystems.
    """
    try:
        return str(Path(project_path).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(project_path).rstrip("/")


def connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=5)
    con.row_factory = sqlite3.Row
    return con


def ensure_session(con: sqlite3.Connection, session_id: str, project_path: str) -> bool:
    """Register a session. Returns True if this call created the row.

    The return value is what gates recall: memory is injected once per
    session, on the first prompt. Injecting on every prompt would append a
    fresh block to the conversation each turn, and the compounding cost would
    swamp anything recall saves.
    """
    project_path = normalize_project(project_path)
    existing = con.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()

    if existing:
        con.execute(
            "UPDATE sessions SET last_active = ? WHERE session_id = ?",
            (_now(), session_id),
        )
        con.commit()
        return False

    con.execute(
        "INSERT INTO sessions (session_id, project_path, started_at, last_active, metadata)"
        " VALUES (?, ?, ?, ?, ?)",
        (session_id, project_path, _now(), _now(), "{}"),
    )
    con.commit()
    return True


def save_nodes(
    db_path: str | Path,
    session_id: str,
    project_path: str,
    nodes: "list[SessionNode]",
) -> int:
    """Persist harvested nodes. Replaces any prior harvest of this session."""
    if not nodes:
        return 0

    con = connect(db_path)
    try:
        ensure_session(con, session_id, normalize_project(project_path))
        # Harvest is idempotent: re-running on the same transcript must not
        # duplicate. This is the bug that filled repo_nodes with 15k dupes.
        con.execute("DELETE FROM nodes WHERE session_id = ?", (session_id,))
        placeholders = ", ".join("?" for _ in NODE_COLUMNS)
        con.executemany(
            f"INSERT INTO nodes ({', '.join(NODE_COLUMNS)}) VALUES ({placeholders})",
            [tuple(n.to_db_row()[c] for c in NODE_COLUMNS) for n in nodes],
        )
        con.commit()
        return len(nodes)
    finally:
        con.close()


def load_project_nodes(
    db_path: str | Path,
    project_path: str,
    exclude_session: str | None = None,
    limit: int = 400,
) -> list[dict]:
    """Load candidate nodes from prior sessions in this project."""
    con = connect(db_path)
    try:
        sql = (
            "SELECT n.* FROM nodes n"
            " JOIN sessions s ON n.session_id = s.session_id"
            " WHERE s.project_path = ? COLLATE NOCASE"
        )
        params: list = [normalize_project(project_path)]
        if exclude_session:
            sql += " AND n.session_id != ?"
            params.append(exclude_session)
        sql += " ORDER BY n.created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        con.close()

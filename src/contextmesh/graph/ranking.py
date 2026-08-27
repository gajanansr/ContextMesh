"""Rank repo_nodes by cross-file reference centrality (PageRank).

The RepoMap benchmark (bench/results/repomap-2026-08-27.json) measured this
tool with the map sorted alphabetically by file path and truncated at a fixed
character budget: cost +45.6% (CI [+0.044, +0.129]), turns not significantly
reduced. The map spent its whole budget on whatever file sorted first, never
on what the task actually needed.

This computes a cheap, real relevance signal instead: a directed graph where
an edge from file A to file B means A's source text references one of B's
top-level symbol names, weighted by how many times. PageRank over that graph
approximates "how central is this file to the codebase" -- the same idea
Aider uses to rank its repo map, without needing a full call-graph resolver.

Not a call graph: a name match is not a verified reference (a symbol named
`get` would pick up noise, which is why short names are excluded). It is a
deliberately cheap heuristic that is still strictly better evidence than file
path alphabetical order, which carries no relevance signal at all.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path

import networkx as nx

logger = logging.getLogger(__name__)

# Below this length a name is too common to be a meaningful reference signal
# ("get", "run", "id") and produces mostly false-positive edges.
MIN_NAME_LENGTH = 4

# Skip scanning files above this size; large generated or data files exist in
# real repos and re-reading them per symbol would dominate index time for no
# ranking signal.
MAX_FILE_BYTES_FOR_SCAN = 512_000

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


async def compute_file_ranks(project_path: str, db) -> dict[str, float]:
    """PageRank score per file_path, summing to ~1.0 across the project.

    Returns {} if there is nothing to rank (no files, or no cross-file
    references were found) so callers can fall back to a neutral order
    rather than treat an empty graph as an error.
    """
    rows = await db.fetchall(
        "SELECT file_path, name, repo_node_type FROM repo_nodes "
        "WHERE project_path = ? COLLATE NOCASE AND file_path IS NOT NULL",
        (project_path,),
    )
    if not rows:
        return {}

    files_by_symbol: dict[str, set[str]] = {}
    all_files: set[str] = set()
    for row in rows:
        fp = row["file_path"]
        all_files.add(fp)
        if row["repo_node_type"] == "repo_file":
            continue
        name = row["name"]
        if name and len(name) >= MIN_NAME_LENGTH:
            files_by_symbol.setdefault(name, set()).add(fp)

    graph = nx.DiGraph()
    graph.add_nodes_from(all_files)

    root = Path(project_path)
    contents: dict[str, str] = {}
    for fp in all_files:
        full = root / fp
        try:
            if full.stat().st_size > MAX_FILE_BYTES_FOR_SCAN:
                continue
            contents[fp] = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

    # Tokenize each file exactly once, then look up only the tokens it
    # actually contains against the symbol table. This keeps the cost close
    # to linear in codebase size rather than O(files x all symbol names).
    for referencing_file, text in contents.items():
        counts = Counter(_TOKEN_RE.findall(text))
        for name, count in counts.items():
            defining_files = files_by_symbol.get(name)
            if not defining_files:
                continue
            targets = defining_files - {referencing_file}
            if not targets:
                continue
            for target in targets:
                if graph.has_edge(referencing_file, target):
                    graph[referencing_file][target]["weight"] += count
                else:
                    graph.add_edge(referencing_file, target, weight=count)

    if graph.number_of_edges() == 0:
        return {}

    try:
        return nx.pagerank(graph, weight="weight")
    except nx.PowerIterationFailedConvergence:
        logger.debug("[Ranking] PageRank did not converge for %s", project_path)
        return {}


async def apply_file_ranks(project_path: str, db) -> int:
    """Compute ranks and store them in each repo_node's metadata.

    Returns the number of nodes updated. Storing the score inside the
    existing JSON `metadata` column avoids an ALTER TABLE migration against
    databases already on disk for existing installs.
    """
    ranks = await compute_file_ranks(project_path, db)
    if not ranks:
        return 0

    import json

    rows = await db.fetchall(
        "SELECT node_id, file_path, metadata FROM repo_nodes "
        "WHERE project_path = ? COLLATE NOCASE",
        (project_path,),
    )

    updates: list[tuple] = []
    for row in rows:
        score = ranks.get(row["file_path"])
        if score is None:
            continue
        try:
            meta = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        meta["rank"] = score
        updates.append((json.dumps(meta), row["node_id"]))

    if updates:
        await db.executemany("UPDATE repo_nodes SET metadata = ? WHERE node_id = ?", updates)
        await db.commit()

    return len(updates)

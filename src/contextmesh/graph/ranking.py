"""Rank code symbols for the RepoMap using query-personalized PageRank.

Two measured failures led here. The map was first ordered alphabetically by
file path (`bench/results/repomap-2026-08-27.json`: cost +45.6%, no significant
turn reduction), then by a static, global PageRank over a name-appears-in-text
graph (`repomap-ranked-2026-08-28.json`: +74.6%, worse). Both spent a fixed
token budget on content chosen without reference to what the user actually
asked for.

This follows the approach Aider uses, which is the one implementation of this
idea with a real track record:

- Edges run from the file *referencing* an identifier to the file *defining*
  it, built from AST identifier nodes rather than raw-text matching, so
  mentions inside strings and comments do not create edges.
- PageRank is *personalized* toward files and identifiers named in the current
  prompt, so the same codebase ranks differently for different questions. This
  is the part a static ranking fundamentally cannot do, and the most likely
  reason the previous attempt lost.
- Identifier weights encode what makes a name informative: distinctive
  multi-word names count for more, private and ubiquitous names count for
  much less, and repeat references are damped by a square root so a single
  hot utility cannot dominate.
- Rank is distributed across each node's out-edges, so the result ranks
  individual *definitions* rather than whole files.

Ranking runs at recall time, not index time, because personalization needs the
prompt. Cost is one PageRank over a graph of the project's files, which is
milliseconds at the scale of a normal repository and bounded by MAX_GRAPH_FILES
above it.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Identifier weighting, following Aider's tuned multipliers.
MENTIONED_IDENT_MULTIPLIER = 10.0   # named in the prompt
DISTINCTIVE_NAME_MULTIPLIER = 10.0  # snake/kebab/camelCase and long enough to be specific
PRIVATE_NAME_MULTIPLIER = 0.1       # leading underscore: internal detail
UBIQUITOUS_NAME_MULTIPLIER = 0.1    # defined in many files: carries little information
DISTINCTIVE_NAME_MIN_LENGTH = 8
UBIQUITOUS_DEFINITION_COUNT = 5

# Above this many files, skip ranking rather than spend real time on PageRank
# during the user's first turn. Falls back to the caller's default ordering.
MAX_GRAPH_FILES = 5_000

# Identifier-shaped tokens in the prompt, used for personalization.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# Path-shaped tokens, e.g. src/foo/bar.py or bar.py
_PATH_RE = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,5}\b")


@dataclass(frozen=True)
class RankedSymbol:
    file_path: str
    name: str
    rank: float


def mentioned_identifiers(prompt: str) -> set[str]:
    return set(_IDENT_RE.findall(prompt or ""))


def mentioned_paths(prompt: str) -> set[str]:
    return set(_PATH_RE.findall(prompt or ""))


def _identifier_multiplier(ident: str, mentioned: set[str], definer_count: int) -> float:
    """How much weight a reference to this identifier carries."""
    mul = 1.0

    if ident in mentioned:
        mul *= MENTIONED_IDENT_MULTIPLIER

    has_alpha = any(c.isalpha() for c in ident)
    is_snake = "_" in ident and has_alpha
    is_kebab = "-" in ident and has_alpha
    is_camel = any(c.isupper() for c in ident) and any(c.islower() for c in ident)
    if (is_snake or is_kebab or is_camel) and len(ident) >= DISTINCTIVE_NAME_MIN_LENGTH:
        mul *= DISTINCTIVE_NAME_MULTIPLIER

    if ident.startswith("_"):
        mul *= PRIVATE_NAME_MULTIPLIER

    if definer_count > UBIQUITOUS_DEFINITION_COUNT:
        mul *= UBIQUITOUS_NAME_MULTIPLIER

    return mul


def _load_graph_inputs(
    con: sqlite3.Connection, project_path: str
) -> tuple[dict[str, set[str]], dict[str, list[tuple[str, int]]], dict[tuple[str, str], list[dict]]]:
    """Read what each file defines and what each file references.

    Returns (defines, references, definitions):
      defines[ident]              -> set of files defining it
      references[ident]           -> [(referencing file, count), ...]
      definitions[(file, ident)]  -> the repo_nodes rows for that definition
    """
    defines: dict[str, set[str]] = defaultdict(set)
    definitions: dict[tuple[str, str], list[dict]] = defaultdict(list)

    rows = con.execute(
        "SELECT name, repo_node_type, file_path, start_line FROM repo_nodes"
        " WHERE project_path = ? COLLATE NOCASE AND file_path IS NOT NULL",
        (project_path,),
    ).fetchall()

    for row in rows:
        node_type = (row["repo_node_type"] or "").lower()
        if "class" in node_type:
            kind = "class"
        elif "function" in node_type or "method" in node_type:
            kind = "def"
        else:
            continue  # file nodes and anything else are not definitions

        name, file_path = row["name"], row["file_path"]
        defines[name].add(file_path)
        definitions[(file_path, name)].append(
            {"file_path": file_path, "name": name, "kind": kind, "start_line": row["start_line"] or 0}
        )

    references: dict[str, list[tuple[str, int]]] = defaultdict(list)
    try:
        ref_rows = con.execute(
            "SELECT file_path, name, ref_count FROM repo_refs WHERE project_path = ? COLLATE NOCASE",
            (project_path,),
        ).fetchall()
    except sqlite3.Error:
        # Project indexed before repo_refs existed. Ranking degrades to the
        # caller's fallback rather than failing the session.
        return defines, {}, definitions

    for row in ref_rows:
        references[row["name"]].append((row["file_path"], row["ref_count"] or 1))

    return defines, references, definitions


def rank_symbols(
    con: sqlite3.Connection,
    project_path: str,
    prompt: str = "",
) -> list[RankedSymbol]:
    """Rank definitions by personalized PageRank. Empty list means "cannot rank"."""
    import networkx as nx

    defines, references, definitions = _load_graph_inputs(con, project_path)
    if not defines or not references:
        return []

    all_files = {f for files in defines.values() for f in files}
    all_files |= {f for refs in references.values() for f, _ in refs}
    if not all_files or len(all_files) > MAX_GRAPH_FILES:
        if len(all_files) > MAX_GRAPH_FILES:
            logger.debug("[Ranking] %d files exceeds MAX_GRAPH_FILES; skipping", len(all_files))
        return []

    mentioned_idents = mentioned_identifiers(prompt)
    mentioned_files = mentioned_paths(prompt)

    # Personalization biases the random walk toward what the prompt is about.
    # Without it every question gets the same map, which is what the previous
    # ranked attempt did and why it did not help.
    personalize = 100.0 / len(all_files)
    personalization: dict[str, float] = {}
    for file_path in all_files:
        score = 0.0
        if any(file_path == m or file_path.endswith("/" + m) for m in mentioned_files):
            score += personalize
        # A path component matching a mentioned identifier counts too, so
        # "fix the scorer" biases toward scorer.py without naming the file.
        parts = set(file_path.replace("\\", "/").split("/"))
        stems = {p.rsplit(".", 1)[0] for p in parts}
        if (parts | stems) & mentioned_idents:
            score += personalize
        if score:
            personalization[file_path] = score

    graph = nx.MultiDiGraph()
    graph.add_nodes_from(all_files)

    for ident, definer_files in defines.items():
        referencing = references.get(ident)
        if not referencing:
            continue
        mul = _identifier_multiplier(ident, mentioned_idents, len(definer_files))
        for referencer, count in referencing:
            # sqrt so a name used 400 times does not outweigh everything else
            weight = mul * math.sqrt(count)
            for definer in definer_files:
                graph.add_edge(referencer, definer, weight=weight, ident=ident)

    # Self-edges for files the prompt named directly.
    #
    # This is a deliberate divergence from Aider. There, personalized files are
    # the ones already open in the chat and are excluded from the map, so their
    # rank usefully flows outward to what they depend on. Here the map is the
    # only context the model gets, so "there's a bug in billing.py" should
    # surface billing.py's own symbols. A self-edge routes the file's
    # personalized rank into its own definitions, which the out-edge
    # distribution below would otherwise never reach for a file that defines
    # things but references nothing.
    if personalization:
        defined_by_file: dict[str, set[str]] = defaultdict(set)
        for ident, definer_files in defines.items():
            for definer in definer_files:
                defined_by_file[definer].add(ident)

        for file_path in personalization:
            for ident in defined_by_file.get(file_path, ()):
                graph.add_edge(
                    file_path, file_path,
                    weight=_identifier_multiplier(ident, mentioned_idents, len(defines[ident])),
                    ident=ident,
                )

    if graph.number_of_edges() == 0:
        return []

    pers_args = (
        {"personalization": personalization, "dangling": personalization}
        if personalization else {}
    )
    try:
        ranked_files = nx.pagerank(graph, weight="weight", **pers_args)
    except (nx.PowerIterationFailedConvergence, ZeroDivisionError):
        try:
            ranked_files = nx.pagerank(graph, weight="weight")
        except (nx.PowerIterationFailedConvergence, ZeroDivisionError):
            logger.debug("[Ranking] PageRank failed to converge for %s", project_path)
            return []

    # Push each file's rank out along its edges, so the score lands on the
    # specific definitions that file depends on rather than on the file.
    ranked_definitions: dict[tuple[str, str], float] = defaultdict(float)
    for source in graph.nodes:
        out_edges = list(graph.out_edges(source, data=True))
        total_weight = sum(data["weight"] for _s, _d, data in out_edges)
        if not total_weight:
            continue
        source_rank = ranked_files.get(source, 0.0)
        for _s, target, data in out_edges:
            ranked_definitions[(target, data["ident"])] += source_rank * data["weight"] / total_weight

    results: list[RankedSymbol] = []
    for (file_path, ident), rank in ranked_definitions.items():
        if (file_path, ident) in definitions:
            results.append(RankedSymbol(file_path=file_path, name=ident, rank=rank))

    results.sort(key=lambda s: (-s.rank, s.file_path, s.name))
    return results


def definition_rows(
    con: sqlite3.Connection, project_path: str
) -> dict[tuple[str, str], list[dict]]:
    """Definition rows keyed by (file_path, name), for rendering ranked output."""
    _defines, _references, definitions = _load_graph_inputs(con, project_path)
    return definitions

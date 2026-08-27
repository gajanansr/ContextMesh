import re
import numpy as np
from datetime import datetime, timezone
from contextmesh.config import RouterConfig
from contextmesh.models.nodes import NodeType

# Words too common to indicate that two texts are about the same thing.
_STOPWORDS = frozenset("""
a an and are as at be but by for from has have how i if in into is it its of on
or that the then there these this to was were what when where which who will
with you your do does did not no yes can could should would please add make use
using file files code line lines run
""".split())

_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")

# Crude suffix stripping so "migration" matches "migrate" and "settings"
# matches "setting". Not a real stemmer, but it costs nothing and recovers the
# most common near-misses.
# Longest first, so "migration" reduces past "ation" rather than stopping at
# "s". Verb and noun forms must land on the same stem or variants never match:
# "migration" -> "migr" is useless unless "migrate" -> "migr" too.
_SUFFIXES = ("ations", "ation", "ates", "ate", "ments", "ment",
             "ings", "ing", "ers", "er", "ies", "ied", "es", "ed", "s")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _terms(text: str) -> set[str]:
    return {
        _stem(w) for w in _WORD.findall((text or "").lower()) if w not in _STOPWORDS
    }

class ContextScorer:
    def __init__(self, config: RouterConfig):
        self.config = config

    def lexical_score(self, query_text: str, node_text: str) -> float:
        """Word-overlap similarity, used when no embedding is available.

        The semantic weight was dead: `query_text` was accepted and ignored,
        and embeddings are never populated, so every node scored identically
        regardless of what the user asked. That made relevance gating
        impossible -- an unrelated prompt and a related one produced the same
        score. This is a cheap stand-in that costs no model load on the recall
        path, which runs before the user's first turn.
        """
        query_words = _terms(query_text)
        node_words = _terms(node_text)
        if not query_words or not node_words:
            return 0.0
        overlap = len(query_words & node_words)
        # Normalised by the query, so a long stored node cannot dilute a
        # strong match, and capped so one repeated word cannot dominate.
        return min(1.0, overlap / len(query_words))

    def recency_score(self, created_at_iso: str) -> float:
        try:
            dt = datetime.fromisoformat(created_at_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = now - dt
            hours = delta.total_seconds() / 3600.0
            
            if hours <= 1.0:
                return 1.0
            elif hours <= 24.0:
                return 0.7
            elif hours <= 168.0:
                return 0.3
            else:
                return 0.1
        except Exception:
            return 0.1

    def file_overlap_score(self, node_files: list[str], task_files: list[str]) -> float:
        if not node_files or not task_files:
            return 0.0
        set_node = set(node_files)
        set_task = set(task_files)
        if not set_node or not set_task:
            return 0.0
        intersection = len(set_node.intersection(set_task))
        union = len(set_node.union(set_task))
        return intersection / union

    def causal_score(self, node_type: str) -> float:
        if node_type in (NodeType.DECISION.value, NodeType.ARCHITECTURE_CHANGE.value):
            return 1.0
        if node_type in (NodeType.BUG.value, NodeType.SOLUTION.value):
            return 0.9
        if node_type == NodeType.FACT.value:
            return 0.7
        return 0.0

    def unresolved_bonus(self, node_type: str) -> float:
        if node_type == NodeType.UNRESOLVED_ISSUE.value:
            return 1.0
        if node_type == NodeType.HYPOTHESIS.value:
            return 0.5
        return 0.0

    def score(self, candidate_nodes: list[dict], current_task_files: list[str], query_embedding: np.ndarray | None, graph_proximity: dict[str, float], query_text: str) -> list[tuple[dict, float]]:
        scored = []
        for node in candidate_nodes:
            # Semantic score: embeddings when present, lexical overlap otherwise.
            semantic = 0.0
            if query_embedding is None:
                semantic = self.lexical_score(
                    query_text,
                    f"{node.get('summary') or ''} {node.get('content') or ''}",
                )
            elif query_embedding is not None and "embedding_vec" in node:
                vec = node["embedding_vec"]
                if vec is not None:
                    # cosine similarity
                    norm1 = np.linalg.norm(query_embedding)
                    norm2 = np.linalg.norm(vec)
                    if norm1 > 0 and norm2 > 0:
                        semantic = np.dot(query_embedding, vec) / (norm1 * norm2)

            node_id = node.get("node_id", "")
            node_type = node.get("node_type", "")
            created_at = node.get("created_at", "")
            
            import json
            files = node.get("files_involved", "[]")
            if isinstance(files, str):
                try:
                    files = json.loads(files)
                except:
                    files = []

            # Compute parts
            val_semantic = semantic
            val_graph = graph_proximity.get(node_id, 0.0)
            val_overlap = self.file_overlap_score(files, current_task_files)
            val_recency = self.recency_score(created_at)
            val_causal = self.causal_score(node_type)
            val_unresolved = self.unresolved_bonus(node_type)

            final_score = (
                self.config.weight_semantic * val_semantic +
                self.config.weight_graph_proximity * val_graph +
                self.config.weight_file_overlap * val_overlap +
                self.config.weight_recency * val_recency +
                self.config.weight_causal * val_causal +
                self.config.weight_unresolved * val_unresolved
            )
            
            scored.append((node, final_score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

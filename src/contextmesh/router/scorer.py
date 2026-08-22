import numpy as np
from datetime import datetime, timezone
from contextmesh.config import RouterConfig
from contextmesh.models.nodes import NodeType

class ContextScorer:
    def __init__(self, config: RouterConfig):
        self.config = config

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
            # Semantic score
            semantic = 0.0
            if query_embedding is not None and "embedding_vec" in node:
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

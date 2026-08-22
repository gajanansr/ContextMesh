import logging
from typing import Optional
import networkx as nx

from contextmesh.models.nodes import SessionNode, MemoryTier
from contextmesh.models.edges import SessionEdge, EdgeType
from contextmesh.store.db import Database

logger = logging.getLogger(__name__)

class SessionGraph:
    def __init__(self, db: Database):
        self.db = db
        self.graph = nx.DiGraph()
        
    async def load_session(self, session_id: str) -> None:
        self.graph.clear()
        
        nodes_data = await self.db.fetchall("SELECT * FROM nodes WHERE session_id = ?", (session_id,))
        for row in nodes_data:
            node = SessionNode.from_db_row(row)
            self.graph.add_node(node.node_id, data=node)
            
        # Assuming edges table has a session_id column or we get all edges where source or target in session nodes.
        # Here we just fetch edges for the loaded nodes.
        node_ids = tuple(row["node_id"] for row in nodes_data)
        if node_ids:
            placeholders = ",".join("?" for _ in node_ids)
            edges_data = await self.db.fetchall(
                f"SELECT * FROM edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                node_ids + node_ids
            )
            for row in edges_data:
                self.graph.add_edge(row["source_id"], row["target_id"], data=row)

    async def add_node(self, node: SessionNode) -> None:
        self.graph.add_node(node.node_id, data=node)
        await self.db.insert("nodes", node.to_db_row())

    async def add_edge(self, edge: SessionEdge) -> None:
        self.graph.add_edge(edge.source_id, edge.target_id, data=edge.to_db_row())
        await self.db.insert("edges", edge.to_db_row())
        
    async def get_neighbors(self, node_id: str, depth: int = 2, edge_types: Optional[list[EdgeType]] = None) -> list[tuple[str, float]]:
        if node_id not in self.graph:
            return []
            
        visited = {node_id: 1.0}
        queue = [(node_id, 0)]
        
        # 1 -> 1.0, 2 -> 0.7, 3 -> 0.4
        def decay(d: int) -> float:
            if d == 1: return 1.0
            if d == 2: return 0.7
            if d == 3: return 0.4
            return 0.1
            
        while queue:
            curr, d = queue.pop(0)
            if d >= depth:
                continue
                
            for neighbor in self.graph.neighbors(curr):
                edge_data = self.graph[curr][neighbor].get("data", {})
                e_type_val = edge_data.get("edge_type")
                if edge_types is not None:
                    if e_type_val not in [et.value for et in edge_types]:
                        continue
                        
                nd = d + 1
                score = decay(nd)
                if neighbor not in visited or visited[neighbor] < score:
                    visited[neighbor] = score
                    queue.append((neighbor, nd))
                    
        return [(n, s) for n, s in visited.items() if n != node_id]

    async def get_nodes_for_task(self, task_id: str) -> list[SessionNode]:
        res = []
        for n, attr in self.graph.nodes(data=True):
            node = attr.get("data")
            if node and node.task_id == task_id:
                res.append(node)
        return res
        
    async def get_hot_nodes(self, session_id: str, limit: int = 50) -> list[SessionNode]:
        res = []
        for n, attr in self.graph.nodes(data=True):
            node = attr.get("data")
            if node and node.session_id == session_id and node.tier == MemoryTier.HOT:
                res.append(node)
        res.sort(key=lambda x: x.importance, reverse=True)
        return res[:limit]
        
    async def get_nodes_by_files(self, file_paths: list[str], session_id: str) -> list[SessionNode]:
        res = []
        for n, attr in self.graph.nodes(data=True):
            node = attr.get("data")
            if node and node.session_id == session_id:
                if any(f in node.files_involved for f in file_paths):
                    res.append(node)
        return res

    async def update_node_tier(self, node_id: str, tier: MemoryTier) -> None:
        if node_id in self.graph:
            node: SessionNode = self.graph.nodes[node_id]["data"]
            node.tier = tier
            
        await self.db.execute("UPDATE nodes SET tier = ? WHERE node_id = ?", (tier.value, node_id))
        await self.db.commit()

    async def mark_task_dormant(self, task_id: str) -> None:
        await self.db.execute("UPDATE tasks SET status = 'dormant' WHERE task_id = ?", (task_id,))
        await self.db.commit()
        
        nodes = await self.get_nodes_for_task(task_id)
        for n in nodes:
            if n.tier == MemoryTier.HOT:
                await self.update_node_tier(n.node_id, MemoryTier.WARM)


_graph: Optional[SessionGraph] = None

def get_session_graph() -> SessionGraph:
    global _graph
    if _graph is None:
        raise RuntimeError("SessionGraph not initialized")
    return _graph

def init_session_graph(db: Database) -> SessionGraph:
    global _graph
    _graph = SessionGraph(db)
    return _graph

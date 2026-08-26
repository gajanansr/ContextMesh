import logging
from pathlib import Path
from typing import Optional
from tree_sitter_language_pack import get_parser

from contextmesh.models.nodes import RepoNode, NodeType
from contextmesh.models.edges import RepoEdge, EdgeType
from contextmesh.store.db import Database

logger = logging.getLogger(__name__)

class RepoGraph:
    def __init__(self, project_path: Path, db: Database):
        self.project_path = project_path
        self.db = db
        self.extensions = {
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "javascript",
            ".jsx": "javascript",
            ".go": "go",
            ".rs": "rust"
        }
        self.skip_dirs = {"node_modules", "__pycache__", ".git", "dist", "build", "venv", ".venv", "site-packages", ".tox"}

    async def index_project(self) -> dict:
        stats = {"files": 0, "functions": 0, "classes": 0, "edges": 0}
        
        # Clear existing nodes for this project to prevent duplicates
        await self.db.execute("DELETE FROM repo_nodes WHERE project_path = ?", (str(self.project_path),))

        for path in self.project_path.rglob("*"):
            if not path.is_file():
                continue
            if any(part in self.skip_dirs for part in path.parts):
                continue
            if path.suffix not in self.extensions:
                continue

            before_fn = await self.db.fetchone("SELECT COUNT(*) as c FROM repo_nodes WHERE repo_node_type='repo_function'")
            before_cl = await self.db.fetchone("SELECT COUNT(*) as c FROM repo_nodes WHERE repo_node_type='repo_class'")
            before_eg = await self.db.fetchone("SELECT COUNT(*) as c FROM repo_edges")

            await self.index_file(path)
            stats["files"] += 1

            after_fn = await self.db.fetchone("SELECT COUNT(*) as c FROM repo_nodes WHERE repo_node_type='repo_function'")
            after_cl = await self.db.fetchone("SELECT COUNT(*) as c FROM repo_nodes WHERE repo_node_type='repo_class'")
            after_eg = await self.db.fetchone("SELECT COUNT(*) as c FROM repo_edges")

            stats["functions"] += (after_fn["c"] - before_fn["c"]) if after_fn and before_fn else 0
            stats["classes"] += (after_cl["c"] - before_cl["c"]) if after_cl and before_cl else 0
            stats["edges"] += (after_eg["c"] - before_eg["c"]) if after_eg and before_eg else 0

        return stats

    async def index_file(self, file_path: Path) -> None:
        lang = self.extensions.get(file_path.suffix)
        if not lang:
            return

        parser = get_parser(lang)
        if not parser:
            return

        try:
            content_bytes = file_path.read_bytes()
        except Exception:
            return

        tree = parser.parse(content_bytes)
        root = tree.root_node

        # Relative path from project root for storage
        try:
            rel_path = str(file_path.relative_to(self.project_path))
        except ValueError:
            rel_path = str(file_path)

        file_node = RepoNode(
            project_path=str(self.project_path),
            repo_node_type=NodeType.REPO_FILE,
            name=file_path.name,
            file_path=rel_path,
            language=lang,
        )
        await self.db.insert("repo_nodes", file_node.to_db_row())

        symbols: list[dict] = []

        def traverse(node):
            if lang == "python":
                if node.type == "function_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        symbols.append({
                            "type": NodeType.REPO_FUNCTION,
                            "name": content_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace"),
                            "start": node.start_point[0],
                            "end": node.end_point[0],
                        })
                elif node.type == "class_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        symbols.append({
                            "type": NodeType.REPO_CLASS,
                            "name": content_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace"),
                            "start": node.start_point[0],
                            "end": node.end_point[0],
                        })
            elif lang in ("javascript", "typescript"):
                if node.type in ("function_declaration", "method_definition"):
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        symbols.append({
                            "type": NodeType.REPO_FUNCTION,
                            "name": content_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace"),
                            "start": node.start_point[0],
                            "end": node.end_point[0],
                        })
                elif node.type == "class_declaration":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        symbols.append({
                            "type": NodeType.REPO_CLASS,
                            "name": content_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace"),
                            "start": node.start_point[0],
                            "end": node.end_point[0],
                        })
            elif lang == "go":
                if node.type in ("function_declaration", "method_declaration"):
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        symbols.append({
                            "type": NodeType.REPO_FUNCTION,
                            "name": content_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace"),
                            "start": node.start_point[0],
                            "end": node.end_point[0],
                        })
            elif lang == "rust":
                if node.type == "function_item":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        symbols.append({
                            "type": NodeType.REPO_FUNCTION,
                            "name": content_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace"),
                            "start": node.start_point[0],
                            "end": node.end_point[0],
                        })

            for child in node.children:
                traverse(child)

        traverse(root)

        for sym in symbols:
            rn = RepoNode(
                project_path=str(self.project_path),
                repo_node_type=sym["type"],
                name=sym["name"],
                file_path=rel_path,
                start_line=sym["start"] + 1,
                end_line=sym["end"] + 1,
                language=lang,
            )
            await self.db.insert("repo_nodes", rn.to_db_row())

            # SAME_FILE edge: symbol → file
            edge = RepoEdge(
                source_id=rn.node_id,
                target_id=file_node.node_id,
                edge_type=EdgeType.SAME_FILE,
            )
            await self.db.insert("repo_edges", edge.to_db_row())

    async def get_file_symbols(self, file_path: str) -> list[dict]:
        """Return raw dicts for all functions/classes in a file."""
        return await self.db.fetchall(
            """SELECT * FROM repo_nodes
               WHERE file_path = ?
               AND repo_node_type IN ('repo_function', 'repo_class', 'repo_method')""",
            (file_path,),
        )

    async def get_callers(self, symbol_name: str) -> list[dict]:
        """Return nodes that call this symbol (v1: empty — requires cross-file resolution)."""
        return []

    async def get_callees(self, symbol_name: str) -> list[dict]:
        """Return nodes this symbol calls (v1: empty — requires cross-file resolution)."""
        return []

    async def get_related_files(self, file_path: str, depth: int = 2) -> list[tuple[str, float]]:
        """
        Return files related to this file by shared repo edges.
        v1: find all symbols in this file, find other files those symbols appear in.
        """
        symbols = await self.get_file_symbols(file_path)
        if not symbols:
            return []

        related: dict[str, float] = {}
        for sym in symbols:
            rows = await self.db.fetchall(
                "SELECT file_path FROM repo_nodes WHERE name = ? AND file_path != ?",
                (sym["name"], file_path),
            )
            for r in rows:
                fp = r.get("file_path") or ""
                if fp:
                    related[fp] = max(related.get(fp, 0.0), 0.7)

        return sorted(related.items(), key=lambda x: x[1], reverse=True)

    async def on_file_changed(self, file_path: str) -> None:
        """Re-index a single file after it was modified."""
        p = Path(self.project_path) / file_path
        if p.exists():
            # Delete old nodes/edges for this file
            old_nodes = await self.db.fetchall(
                "SELECT node_id FROM repo_nodes WHERE file_path = ?", (file_path,)
            )
            for n in old_nodes:
                await self.db.execute(
                    "DELETE FROM repo_edges WHERE source_id = ? OR target_id = ?",
                    (n["node_id"], n["node_id"]),
                )
            await self.db.execute(
                "DELETE FROM repo_nodes WHERE file_path = ?", (file_path,)
            )
            await self.db.commit()
            await self.index_file(p)
            logger.info("Re-indexed %s", file_path)


_repo_graph: Optional[RepoGraph] = None


def get_repo_graph() -> RepoGraph:
    global _repo_graph
    if _repo_graph is None:
        raise RuntimeError("RepoGraph not initialized — call init_repo_graph() first")
    return _repo_graph


def init_repo_graph(project_path: Path, db) -> RepoGraph:
    """Synchronous init — indexing happens separately via index_project()."""
    global _repo_graph
    _repo_graph = RepoGraph(project_path, db)
    return _repo_graph


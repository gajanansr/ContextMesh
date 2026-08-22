import json
import tiktoken
from typing import Any
from contextmesh.config import RouterConfig
from contextmesh.models.nodes import ContextRequest, ContextResponse, NodeType

class ContextAssembler:
    def __init__(self, config: RouterConfig, db: Any, embedding_store: Any, session_graph: Any, repo_graph: Any):
        self.config = config
        self.db = db
        self.embedding_store = embedding_store
        self.session_graph = session_graph
        self.repo_graph = repo_graph
        self.encoder = tiktoken.get_encoding("cl100k_base")

    def _count_tokens(self, text: str) -> int:
        return len(self.encoder.encode(text, disallowed_special=()))

    async def _format_node(self, node: dict) -> str:
        ntype = node.get("node_type", "")
        emoji = "📌"
        if ntype == NodeType.DECISION.value: emoji = "🔴"
        elif ntype == NodeType.BUG.value: emoji = "🐛"
        elif ntype == NodeType.SOLUTION.value: emoji = "✅"
        elif ntype == NodeType.ERROR.value: emoji = "❌"
        elif ntype == NodeType.FILE_MODIFICATION.value: emoji = "📝"
        elif ntype == NodeType.UNRESOLVED_ISSUE.value: emoji = "⚠️"

        content = node.get("summary") or node.get("content") or ""
        files = node.get("files_involved", "[]")
        if isinstance(files, str):
            try: files = json.loads(files)
            except: files = []
        
        created_at = node.get("created_at", "")
        # Very basic relative time formatting for brevity
        time_str = created_at
        
        res = f"{emoji} [{ntype.upper()}] {time_str}\n{content}"
        if files:
            res += f"\nFiles: {', '.join(files)}"
        return res

    async def _format_repo_node(self, repo_node: dict) -> str:
        ntype = repo_node.get("repo_node_type", "repo_symbol")
        name = repo_node.get("name", "unknown")
        filepath = repo_node.get("file_path", "")
        startline = repo_node.get("start_line", "")
        loc = f"{filepath}:{startline}" if filepath and startline else filepath
        sig = repo_node.get("signature") or repo_node.get("docstring") or ""
        if len(sig) > 200:
            sig = sig[:197] + "..."
        return f"- {ntype} `{name}` ({loc})\n  {sig}"

    async def assemble(self, request: ContextRequest) -> ContextResponse:
        from contextmesh.router.scorer import ContextScorer
        scorer = ContextScorer(self.config)

        # 1. Get current task
        task = await self.db.get_active_task(request.session_id)
        task_id = task.get("task_id") if task else None
        task_name = task.get("name") if task else "Default Task"
        task_files = request.files_hint or []
        if task:
            try:
                tf = json.loads(task.get("files_involved", "[]"))
                if isinstance(tf, list): task_files.extend(tf)
            except: pass

        # 2. Accumulated session tokens
        accum_tokens = await self.db.get_cumulative_tokens(request.session_id)

        # 3. Query embedding
        query_emb = None
        if request.task_hint and self.embedding_store:
            query_emb = await self.embedding_store.get_embedding(request.task_hint)

        # 4. Get hot nodes
        hot_nodes = await self.db.get_session_nodes(request.session_id, tier="hot", task_id=task_id)
        
        # 5. BFS for graph proximity
        graph_proximity = {}
        if self.session_graph and task_id:
            # Fake proximity for stub
            graph_proximity = {n["node_id"]: 1.0 for n in hot_nodes}

        # 6. Score warm nodes
        warm_nodes = await self.db.get_session_nodes(request.session_id, tier="warm")
        # Inject embeddings
        if self.embedding_store:
            for n in warm_nodes:
                n["embedding_vec"] = await self.embedding_store.get_node_embedding(n["node_id"])

        scored_warm = scorer.score(warm_nodes, task_files, query_emb, graph_proximity, request.task_hint or "")

        # Categorize scored nodes
        decisions = [n for n, s in scored_warm if n["node_type"] == NodeType.DECISION.value]
        unresolved = [n for n, s in scored_warm if n["node_type"] == NodeType.UNRESOLVED_ISSUE.value]
        recent_warm = [n for n, s in scored_warm if n["node_type"] not in (NodeType.DECISION.value, NodeType.UNRESOLVED_ISSUE.value)]

        # 7. Repo nodes (skip proper graph retrieval for now, just mock)
        repo_nodes = []
        if self.repo_graph and task_files:
            # fetch from db directly for simplicity
            for f in task_files:
                nodes = await self.db.fetchall("SELECT * FROM repo_nodes WHERE file_path = ?", (f,))
                repo_nodes.extend(nodes)

        # 8. Assemble Context Text
        budget = request.budget_tokens
        current_tokens = 0
        context_parts = []
        included_ids = []
        
        counts = {"hot": 0, "warm": 0, "repo": 0, "cold": 0}

        def add_section(title, items, formatter, tier="warm"):
            nonlocal current_tokens
            if not items or current_tokens >= budget:
                return
            section_str = f"=== {title} ===\n"
            context_parts.append(section_str)
            current_tokens += self._count_tokens(section_str)
            for item in items:
                if current_tokens >= budget:
                    break
                # item can be dict or tuple(dict, float)
                node_dict = item[0] if isinstance(item, tuple) else item
                import asyncio
                # running format sync for simplicity in loop
                # In real code use asyncio.gather or await in async generator
                # Since we are in async method:
                pass # formatted below properly
            
        # Proper async building
        async def build_section(title, items, formatter_coro, tier="warm"):
            nonlocal current_tokens
            if not items or current_tokens >= budget:
                return
            section_str = f"=== {title} ===\n"
            context_parts.append(section_str)
            current_tokens += self._count_tokens(section_str)
            for item in items:
                if current_tokens >= budget:
                    break
                node_dict = item[0] if isinstance(item, tuple) else item
                text = await formatter_coro(node_dict) + "\n\n"
                toks = self._count_tokens(text)
                if current_tokens + toks <= budget:
                    context_parts.append(text)
                    current_tokens += toks
                    if "node_id" in node_dict:
                        included_ids.append(node_dict["node_id"])
                    counts[tier] += toks

        await build_section(f"CURRENT TASK: {task_name}", hot_nodes, self._format_node, "hot")
        await build_section("RELEVANT DECISIONS", decisions, self._format_node, "warm")
        await build_section("RELATED CODE CONTEXT", repo_nodes, self._format_repo_node, "repo")
        await build_section("RECENT HISTORY", recent_warm, self._format_node, "warm")
        await build_section("UNRESOLVED ISSUES", unresolved, self._format_node, "warm")

        final_text = "".join(context_parts)
        
        return ContextResponse(
            session_id=request.session_id,
            task_id=task_id,
            task_name=task_name,
            context_text=final_text,
            total_tokens=current_tokens,
            hot_tokens=counts["hot"],
            warm_tokens=counts["warm"],
            repo_tokens=counts["repo"],
            cold_tokens=counts["cold"],
            included_node_ids=included_ids,
            accumulated_session_tokens=accum_tokens,
            tokens_saved=max(0, accum_tokens - current_tokens),
            compression_ratio=(current_tokens / accum_tokens) if accum_tokens > 0 else 1.0
        )

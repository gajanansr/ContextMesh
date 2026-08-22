"""Task hierarchy manager — handles Project > Feature > Thread structure."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from contextmesh.models.nodes import Task, TaskType, MemoryTier, TaskStatus

logger = logging.getLogger(__name__)


class TaskHierarchy:
    def __init__(self, db, session_graph) -> None:
        self.db = db
        self.session_graph = session_graph

    async def get_or_create_session(self, session_id: str, project_path: str) -> None:
        sess = await self.db.get_session(session_id)
        if not sess:
            await self.db.upsert_session(session_id, project_path)

    async def get_active_task(self, session_id: str) -> Optional[Task]:
        row = await self.db.get_active_task(session_id)
        return Task.from_db_row(row) if row else None

    async def get_or_create_active_task(self, session_id: str, project_path: str = "") -> Task:
        """
        Get the current hot task, or create a first task for this session.
        This is the main entry point used by event handlers.
        """
        # Ensure session exists
        sess = await self.db.get_session(session_id)
        if not sess:
            await self.db.upsert_session(session_id, project_path or "")

        task = await self.get_active_task(session_id)
        if task:
            return task

        # First task for this session
        task = await self.create_task(session_id, name="initial task")
        return task

    async def create_task(
        self,
        session_id: str,
        name: str,
        parent_task_id: Optional[str] = None,
        task_type: TaskType = TaskType.THREAD,
    ) -> Task:
        now = datetime.now(timezone.utc).isoformat()
        task = Task(
            session_id=session_id,
            name=name,
            parent_task_id=parent_task_id,
            task_type=task_type,
            status=TaskStatus.ACTIVE,
            tier=MemoryTier.HOT,
            started_at=now,
            last_active=now,
        )
        await self.db.insert("tasks", task.to_db_row())
        return task

    async def switch_task(self, session_id: str, new_task_name: str) -> Task:
        """Mark current hot task dormant/warm, create new hot task."""
        active = await self.get_active_task(session_id)
        if active:
            await self.session_graph.mark_task_dormant(active.task_id)

        return await self.create_task(session_id, new_task_name)

    async def get_all_tasks(self, session_id: str) -> list[Task]:
        rows = await self.db.fetchall(
            "SELECT * FROM tasks WHERE session_id = ? ORDER BY last_active DESC",
            (session_id,),
        )
        return [Task.from_db_row(r) for r in rows]

    async def update_task_files(self, task_id: str, new_files: list[str]) -> None:
        if not new_files:
            return
        row = await self.db.fetchone(
            "SELECT files_involved FROM tasks WHERE task_id = ?", (task_id,)
        )
        if not row:
            return
        current = json.loads(row.get("files_involved") or "[]")
        merged = list(set(current + new_files))
        await self.db.execute(
            "UPDATE tasks SET files_involved = ? WHERE task_id = ?",
            (json.dumps(merged), task_id),
        )
        await self.db.commit()

    async def promote_task(self, task_id: str) -> None:
        """Move a warm/cold task back to hot (when user references old work)."""
        await self.db.execute(
            "UPDATE tasks SET tier = ?, status = ? WHERE task_id = ?",
            (MemoryTier.HOT.value, TaskStatus.ACTIVE.value, task_id),
        )
        await self.db.commit()

    async def demote_old_tasks(
        self, session_id: str, keep_hot_task_id: Optional[str] = None
    ) -> None:
        """
        After a Stop event, move stale tasks to warm/cold based on recency.
        Tasks active in the last warm_window_hours stay warm; older go cold.
        """
        from contextmesh.config import get_config
        cfg = get_config()
        cutoff_hours = cfg.tasks.warm_window_hours

        rows = await self.db.fetchall(
            "SELECT * FROM tasks WHERE session_id = ?", (session_id,)
        )
        now = datetime.now(timezone.utc)

        for row in rows:
            tid = row["task_id"]
            if keep_hot_task_id and tid == keep_hot_task_id:
                continue
            if row.get("tier") == MemoryTier.HOT.value:
                last_active_str = row.get("last_active") or row.get("started_at", "")
                try:
                    last_dt = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
                    hours_ago = (now - last_dt).total_seconds() / 3600
                    new_tier = MemoryTier.WARM if hours_ago < cutoff_hours else MemoryTier.COLD
                except Exception:
                    new_tier = MemoryTier.WARM

                await self.db.execute(
                    "UPDATE tasks SET tier = ?, status = ? WHERE task_id = ?",
                    (new_tier.value, TaskStatus.DORMANT.value, tid),
                )

        await self.db.commit()

    async def get_task_summary_text(self, task_id: str) -> str:
        """Format a task's most important nodes as readable context text."""
        nodes = await self.session_graph.get_nodes_for_task(task_id)
        if not nodes:
            return "No content recorded for this task yet."

        # Most important first, last 8 nodes
        nodes.sort(key=lambda x: (x.importance, x.created_at), reverse=True)
        lines = []
        for n in nodes[:8]:
            text = n.summary or n.content
            preview = text[:150].replace("\n", " ") + ("…" if len(text) > 150 else "")
            lines.append(f"[{n.node_type.value}] {preview}")

        return "\n".join(lines)


# ── Singleton ─────────────────────────────────────────────────────────────────

_hierarchy: Optional[TaskHierarchy] = None


def get_hierarchy() -> TaskHierarchy:
    global _hierarchy
    if _hierarchy is None:
        raise RuntimeError("TaskHierarchy not initialized — call init_hierarchy() first")
    return _hierarchy


def init_hierarchy(db, session_graph) -> TaskHierarchy:
    global _hierarchy
    _hierarchy = TaskHierarchy(db, session_graph)
    return _hierarchy

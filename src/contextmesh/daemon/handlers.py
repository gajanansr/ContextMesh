"""Event handlers that process Claude Code hook events and build the session graph."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import tiktoken

from contextmesh.config import Config
from contextmesh.models.edges import EdgeType, SessionEdge
from contextmesh.models.nodes import (
    HookEvent, HookEventType, MemoryTier, NodeType, SessionNode,
)
from contextmesh.store.db import Database

logger = logging.getLogger(__name__)

# Token encoder — loaded once
_enc = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    try:
        return len(_enc.encode(text, disallowed_special=()))
    except Exception:
        return len(text) // 4  # rough fallback


class EventHandler:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        # Per-session last node id — used to chain FOLLOWED_BY edges
        self._last_node_id: dict[str, str] = {}

    async def handle(self, event: HookEvent) -> None:
        """Main dispatch — catches all exceptions so the daemon never crashes."""
        try:
            if event.event_type == HookEventType.USER_PROMPT_SUBMIT:
                await self._handle_user_prompt(event)
            elif event.event_type == HookEventType.PRE_TOOL_USE:
                await self._handle_pre_tool_use(event)
            elif event.event_type == HookEventType.POST_TOOL_USE:
                await self._handle_post_tool_use(event)
            elif event.event_type == HookEventType.STOP:
                await self._handle_stop(event)
            elif event.event_type == HookEventType.NOTIFICATION:
                await self._handle_notification(event)
            else:
                logger.debug("Unhandled event type: %s", event.event_type)
        except Exception:
            logger.exception(
                "Error handling event %s for session %s",
                event.event_type, event.session_id,
            )

    # ──────────────────────────────────────────────────────────────────────────
    # UserPromptSubmit
    # ──────────────────────────────────────────────────────────────────────────

    async def _handle_user_prompt(self, event: HookEvent) -> None:
        if not event.prompt:
            return

        prompt = event.prompt
        token_count = _count_tokens(prompt)

        # 1. Record in accumulator (baseline tracking)
        await self.db.record_accumulator_turn(
            event.session_id, "user", token_count, prompt[:200]
        )

        # 2. Get/create active task
        task = None
        try:
            from contextmesh.tasks.hierarchy import get_hierarchy
            hierarchy = get_hierarchy()
            task = await hierarchy.get_or_create_active_task(
                event.session_id, event.project_path
            )
        except (RuntimeError, Exception) as e:
            logger.debug("Hierarchy not ready: %s", e)

        # 3. Extract files mentioned in the prompt
        files: list[str] = []
        try:
            from contextmesh.tasks.detector import get_detector
            files = await get_detector().extract_files_from_content(prompt, None)
        except RuntimeError:
            pass

        # 4. Check for task boundary
        if task:
            try:
                from contextmesh.tasks.detector import get_detector
                detector = get_detector()
                should_switch, confidence = await detector.should_create_new_task(
                    event.session_id, prompt, files
                )
                if should_switch and confidence > 0.7:
                    from contextmesh.tasks.hierarchy import get_hierarchy
                    task_name = await detector.detect_task_name(prompt, files)
                    task = await get_hierarchy().switch_task(event.session_id, task_name)
                    logger.info(
                        "Task switched → '%s' (confidence=%.2f)", task.name, confidence
                    )
            except RuntimeError:
                pass

        # 5. Build and persist the node
        node = SessionNode(
            session_id=event.session_id,
            task_id=task.task_id if task else None,
            node_type=NodeType.USER_PROMPT,
            content=prompt,
            files_involved=files,
            token_count=token_count,
            importance=0.50,
        )
        await self.db.insert("nodes", node.to_db_row())

        # 6. Chain FOLLOWED_BY edge from previous node
        await self._chain_edge(event.session_id, node.node_id)

        # 7. Update task files
        if task and files:
            try:
                from contextmesh.tasks.hierarchy import get_hierarchy
                await get_hierarchy().update_task_files(task.task_id, files)
            except RuntimeError:
                pass

        # 8. Store embedding asynchronously (non-blocking)
        asyncio.create_task(self._store_embedding_async(node))

    # ──────────────────────────────────────────────────────────────────────────
    # PreToolUse
    # ──────────────────────────────────────────────────────────────────────────

    async def _handle_pre_tool_use(self, event: HookEvent) -> None:
        if not event.tool_input:
            return

        input_str = json.dumps(event.tool_input)
        token_count = _count_tokens(input_str)

        await self.db.record_accumulator_turn(
            event.session_id, "tool_input", token_count, input_str[:200]
        )

        # Extract files from tool_input and update task
        files = _extract_files_from_tool_input(event.tool_input)
        if files:
            task_row = await self.db.get_active_task(event.session_id)
            if task_row:
                try:
                    from contextmesh.tasks.hierarchy import get_hierarchy
                    await get_hierarchy().update_task_files(task_row["task_id"], files)
                except RuntimeError:
                    pass

    # ──────────────────────────────────────────────────────────────────────────
    # PostToolUse
    # ──────────────────────────────────────────────────────────────────────────

    async def _handle_post_tool_use(self, event: HookEvent) -> None:
        response = event.tool_response or ""
        if not response:
            return

        token_count = _count_tokens(response)

        # 1. Accumulator (baseline)
        await self.db.record_accumulator_turn(
            event.session_id, "tool_result", token_count, response[:200]
        )

        # 2. Classify node type
        node_type = NodeType.TOOL_RESULT
        importance = 0.35
        try:
            from contextmesh.tasks.detector import get_detector
            node_type = await get_detector().classify_node_type(event)
            importance = await get_detector().get_importance_score(node_type, response)
        except RuntimeError:
            # Fallback classification without detector
            node_type = _fallback_classify(event)
            importance = 0.5

        # 3. Exit-code override
        if event.tool_exit_code is not None and event.tool_exit_code != 0:
            node_type = NodeType.ERROR
            importance = 0.80

        # 4. Extract files
        files = _extract_files_from_tool_input(event.tool_input or {})
        try:
            from contextmesh.tasks.detector import get_detector
            extra = await get_detector().extract_files_from_content(response, event.tool_input)
            files = list(set(files + extra))
        except RuntimeError:
            pass

        # 5. Active task id
        task_row = await self.db.get_active_task(event.session_id)
        task_id = task_row["task_id"] if task_row else None

        # 6. Build and persist node
        node = SessionNode(
            session_id=event.session_id,
            task_id=task_id,
            node_type=node_type,
            content=response,
            files_involved=files,
            token_count=token_count,
            importance=importance,
            metadata={
                "tool_name": event.tool_name or "",
                "exit_code": event.tool_exit_code,
            },
        )
        await self.db.insert("nodes", node.to_db_row())

        # 7. Chain FOLLOWED_BY edge
        await self._chain_edge(event.session_id, node.node_id)

        # 8. Update task files
        if task_id and files:
            try:
                from contextmesh.tasks.hierarchy import get_hierarchy
                await get_hierarchy().update_task_files(task_id, files)
            except RuntimeError:
                pass

        # 9. Async embedding
        asyncio.create_task(self._store_embedding_async(node))

    # ──────────────────────────────────────────────────────────────────────────
    # Stop
    # ──────────────────────────────────────────────────────────────────────────

    async def _handle_stop(self, event: HookEvent) -> None:
        await self.db.touch_session(event.session_id)
        try:
            from contextmesh.tasks.hierarchy import get_hierarchy
            # Get current hot task id to preserve it
            task_row = await self.db.get_active_task(event.session_id)
            keep_id = task_row["task_id"] if task_row else None
            await get_hierarchy().demote_old_tasks(event.session_id, keep_id)
        except RuntimeError:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # Notification (used by MCP record_decision tool)
    # ──────────────────────────────────────────────────────────────────────────

    async def _handle_notification(self, event: HookEvent) -> None:
        meta = event.metadata or {}
        decision_content = meta.get("decision", "")
        if not decision_content:
            return

        task_row = await self.db.get_active_task(event.session_id)
        task_id = task_row["task_id"] if task_row else None

        files = meta.get("files", [])
        if isinstance(files, str):
            files = [f.strip() for f in files.split(",") if f.strip()]

        symbols = meta.get("symbols", [])
        if isinstance(symbols, str):
            symbols = [s.strip() for s in symbols.split(",") if s.strip()]

        consequence = meta.get("consequence", "")
        full_content = decision_content
        if consequence:
            full_content += f"\n\nConsequence: {consequence}"

        node = SessionNode(
            session_id=event.session_id,
            task_id=task_id,
            node_type=NodeType.DECISION,
            content=full_content,
            files_involved=files,
            symbols=symbols,
            confidence=float(meta.get("confidence", 0.9)),
            importance=0.90,
            token_count=_count_tokens(full_content),
        )
        await self.db.insert("nodes", node.to_db_row())
        await self._chain_edge(event.session_id, node.node_id)
        asyncio.create_task(self._store_embedding_async(node))
        logger.info("Decision recorded: %s…", decision_content[:60])

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _chain_edge(self, session_id: str, new_node_id: str) -> None:
        """Add a FOLLOWED_BY edge from the previous node to this one."""
        last_id = self._last_node_id.get(session_id)
        if last_id and last_id != new_node_id:
            edge = SessionEdge(
                source_id=last_id,
                target_id=new_node_id,
                edge_type=EdgeType.FOLLOWED_BY,
                weight=0.4,
            )
            try:
                await self.db.insert("edges", edge.to_db_row())
            except Exception as e:
                logger.debug("Edge insert failed: %s", e)
        self._last_node_id[session_id] = new_node_id

    async def _store_embedding_async(self, node: SessionNode) -> None:
        """Store embedding in background — uses the correct method name."""
        try:
            from contextmesh.embeddings.store import get_store
            store = get_store()
            text = node.content[:2000]  # cap to avoid very long tool outputs
            await store.store_node_embedding(node.node_id, "nodes", text)
        except RuntimeError:
            pass  # Store not initialized yet — fine
        except Exception as e:
            logger.debug("Embedding storage failed for %s: %s", node.node_id, e)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_files_from_tool_input(tool_input: dict) -> list[str]:
    """Fast, sync file extraction from tool_input dict."""
    files: set[str] = set()
    for key in ("path", "file_path", "filename", "target_file", "file"):
        if val := tool_input.get(key):
            if isinstance(val, str) and val.strip():
                files.add(val.strip())
    return list(files)


def _fallback_classify(event: HookEvent) -> NodeType:
    """Simple fallback classifier when TaskDetector not initialized."""
    tool = event.tool_name or ""
    content = (event.tool_response or "").lower()
    if tool in ("write_file", "edit_file", "str_replace_editor", "str_replace"):
        return NodeType.FILE_MODIFICATION
    if tool in ("read_file", "view_file"):
        return NodeType.FILE_READ
    if any(k in content for k in ("error", "exception", "traceback")):
        return NodeType.ERROR
    return NodeType.TOOL_RESULT


# ── Singleton ──────────────────────────────────────────────────────────────────

_handler: Optional[EventHandler] = None


def init_handler(db: Database, config: Config) -> EventHandler:
    global _handler
    _handler = EventHandler(db, config)
    return _handler


def get_handler() -> EventHandler:
    global _handler
    if _handler is None:
        raise RuntimeError("EventHandler not initialized — call init_handler() first")
    return _handler


async def handle_hook_event(event: HookEvent) -> None:
    await get_handler().handle(event)

"""Task boundary detection using multi-signal approach."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from contextmesh.config import TasksConfig
from contextmesh.models.nodes import HookEvent, NodeType

logger = logging.getLogger(__name__)

# Stopwords for task name extraction
_STOPWORDS = {
    "the", "is", "at", "which", "on", "a", "an", "and", "in", "to",
    "for", "of", "with", "it", "this", "that", "i", "we", "my", "me",
    "can", "you", "fix", "let", "do", "get", "now", "please", "just",
}

# Explicit task-switch signals in user prompts
_SWITCH_SIGNALS = [
    "now let's", "now lets", "let's work on", "lets work on",
    "switch to", "move on", "new task", "different task",
    "next thing", "next up", "forget that", "stop that",
    "instead", "actually,", "nvm,", "nevermind",
]


class TaskDetector:
    def __init__(self, config: TasksConfig, embedding_store, db) -> None:
        self.config = config
        self.embedding_store = embedding_store
        self.db = db

    async def should_create_new_task(
        self,
        session_id: str,
        new_content: str,
        files_touched: list[str] | None = None,
    ) -> tuple[bool, float]:
        """
        Returns (should_switch, confidence).
        Combines 4 signals: explicit phrase, file overlap, embedding distance, turn guard.
        Fails safe: returns False on any error.
        """
        files_touched = files_touched or []
        try:
            active_task = await self.db.get_active_task(session_id)

            # No task yet → create first one
            if not active_task:
                return True, 1.0

            task_id = active_task["task_id"]

            # Guard: don't switch if current task has too few turns
            nodes = await self.db.get_session_nodes(
                session_id, task_id=task_id, limit=self.config.min_turns_per_task
            )
            if len(nodes) < self.config.min_turns_per_task:
                return False, 0.0

            content_lower = new_content.lower()

            # Signal 1: explicit switch phrase (high confidence)
            if any(sig in content_lower for sig in _SWITCH_SIGNALS):
                return True, 0.9

            # Signal 2: file overlap — new files with zero overlap to current task
            if files_touched:
                task_files = json.loads(active_task.get("files_involved") or "[]")
                if task_files and not set(files_touched).intersection(set(task_files)):
                    return True, 0.75

            # Signal 3: embedding distance (most expensive — do last)
            if self.embedding_store is not None:
                try:
                    recent = await self.db.get_session_nodes(
                        session_id, task_id=task_id, limit=5
                    )
                    recent_texts = [n["content"] for n in recent if n.get("content")]
                    if recent_texts:
                        combined = " ".join(recent_texts)
                        recent_vec = (await self.embedding_store.encode([combined]))[0]
                        new_vec = (await self.embedding_store.encode([new_content]))[0]
                        dist = await self.embedding_store.cosine_distance(recent_vec, new_vec)
                        if dist > self.config.topic_shift_threshold:
                            return True, min(1.0, dist)
                except Exception as e:
                    logger.debug("Embedding distance check failed: %s", e)

            return False, 0.0

        except Exception as e:
            logger.warning("should_create_new_task error: %s", e)
            return False, 0.0

    async def detect_task_name(self, content: str, files: list[str] | None = None) -> str:
        """Extract a short 3-6 word task name from user content."""
        files = files or []
        words = re.findall(r"\b[a-zA-Z]\w+\b", content)
        filtered = [w.lower() for w in words if w.lower() not in _STOPWORDS and len(w) > 2]
        name_words = filtered[:5]
        name = " ".join(name_words) if name_words else "new task"

        if files:
            stem = Path(files[0]).stem
            name = f"{name} ({stem})"

        return name.strip() or "new task"

    async def classify_node_type(
        self,
        event: HookEvent,
    ) -> NodeType:
        """
        Classify a hook event into a NodeType.
        Accepts the full HookEvent to avoid signature mismatches.
        """
        content = event.tool_response or event.prompt or ""
        tool_name = event.tool_name
        content_lower = content.lower()

        if event.event_type.value == "UserPromptSubmit":
            return NodeType.USER_PROMPT

        if tool_name in ("bash", "computer", "execute_command", "run_command"):
            if event.tool_exit_code is not None and event.tool_exit_code != 0:
                return NodeType.ERROR
            if any(k in content_lower for k in ("error", "exception", "traceback", "failed")):
                return NodeType.ERROR
            if "test" in content_lower and any(k in content_lower for k in ("pass", "fail", "ok", "error")):
                return NodeType.TEST_RESULT
            return NodeType.TOOL_RESULT

        if tool_name in ("read_file", "view_file", "cat", "open_file", "ReadFile"):
            return NodeType.FILE_READ

        if tool_name in (
            "write_file", "edit_file", "str_replace_editor", "create_file",
            "WriteFile", "str_replace", "insert_content",
        ):
            return NodeType.FILE_MODIFICATION

        if any(k in content_lower for k in ("error", "exception", "traceback")):
            return NodeType.ERROR

        if "test" in content_lower and any(k in content_lower for k in ("passed", "failed", "ok")):
            return NodeType.TEST_RESULT

        return NodeType.TOOL_RESULT

    async def extract_files_from_content(
        self,
        content: str,
        tool_input: dict | None = None,
    ) -> list[str]:
        """Extract file paths from tool_input dict and content text."""
        files: set[str] = set()

        # Priority 1: structured tool input fields
        if tool_input:
            for key in ("path", "file_path", "filename", "target_file", "file"):
                if val := tool_input.get(key):
                    if isinstance(val, str) and val.strip():
                        files.add(val.strip())

        # Priority 2: regex on content — paths ending in known source extensions
        ext_pattern = r"(?:[\w./\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|cpp|c|h|rb|cs|swift|kt|md|yaml|toml|json))"
        for match in re.finditer(ext_pattern, content):
            p = match.group(0)
            # Filter out noise (URLs, very short matches)
            if len(p) > 4 and not p.startswith("http"):
                files.add(p)

        return list(files)

    async def get_importance_score(self, node_type: NodeType, content: str) -> float:
        """Return 0.0–1.0 importance score for a node type."""
        scores = {
            NodeType.DECISION: 0.90,
            NodeType.ARCHITECTURE_CHANGE: 0.90,
            NodeType.BUG: 0.85,
            NodeType.SOLUTION: 0.85,
            NodeType.UNRESOLVED_ISSUE: 0.80,
            NodeType.ERROR: 0.75,
            NodeType.FILE_MODIFICATION: 0.70,
            NodeType.TEST_RESULT: 0.65,
            NodeType.FACT: 0.60,
            NodeType.USER_PROMPT: 0.50,
            NodeType.ASSISTANT_RESPONSE: 0.45,
            NodeType.FILE_READ: 0.40,
            NodeType.TOOL_RESULT: 0.35,
        }
        return scores.get(node_type, 0.50)


# ── Singleton ─────────────────────────────────────────────────────────────────

_detector: Optional[TaskDetector] = None


def get_detector() -> TaskDetector:
    global _detector
    if _detector is None:
        raise RuntimeError("TaskDetector not initialized — call init_detector() first")
    return _detector


def init_detector(config: TasksConfig, embedding_store, db) -> TaskDetector:
    global _detector
    _detector = TaskDetector(config, embedding_store, db)
    return _detector

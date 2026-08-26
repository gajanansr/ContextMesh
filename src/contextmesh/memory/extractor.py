"""Turn a finished session transcript into typed knowledge nodes.

Extraction is deterministic and reads only the local transcript -- no model
call, so harvesting costs nothing and cannot fail on a rate limit or an
expired login at session end.

That constrains what can honestly be extracted. Goals, file modifications,
errors, commits and test outcomes are all directly observable. Decisions and
hypotheses are not; inferring them from raw text would produce confident
noise, and a memory that misremembers is worse than no memory. `enrich_*`
hooks are the seam where a model-backed pass can add them later.

The error nodes matter most: an error whose file is never successfully
modified afterwards is a dead end, and dead ends are exactly what a fresh
session repeats.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

from contextmesh.models.nodes import MemoryTier, NodeType, SessionNode

# Tools whose use means a file changed.
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Bash commands that indicate a test run, matched as substrings.
TEST_MARKERS = (
    "pytest", "npm test", "npm run test", "yarn test", "go test",
    "cargo test", "jest", "vitest", "unittest", "rspec", "mvn test",
)

# Prompts shorter than this, or matching a filler phrase, carry no recallable
# intent. Storing them dilutes the score ranking with noise.
MIN_PROMPT_CHARS = 15
FILLER_PROMPTS = {
    "retry", "again", "continue", "go on", "go ahead", "ok", "okay", "yes",
    "no", "sure", "thanks", "next", "stop", "wait", "hmm", "y", "n",
}

# Content over this many characters is stored truncated. Nodes are recalled
# into a live prompt, so an unbounded blob would defeat the purpose.
MAX_CONTENT_CHARS = 2_000


def _truncate(text: str, limit: int = MAX_CONTENT_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def _iter_records(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _blocks(record: dict) -> list[dict]:
    content = (record.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text_of(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _is_meaningful_prompt(text: str) -> bool:
    """Filter filler so recall ranks intent, not acknowledgements."""
    stripped = (text or "").strip()
    if stripped.startswith("<"):          # system-injected, not a human ask
        return False
    if stripped.lower().strip(" .!?") in FILLER_PROMPTS:
        return False
    return len(stripped) >= MIN_PROMPT_CHARS


def _is_test_command(command: str) -> bool:
    lowered = command.lower()
    return any(marker in lowered for marker in TEST_MARKERS)


def extract_nodes(
    transcript_path: str | Path,
    session_id: str,
    project_path: str,
) -> list[SessionNode]:
    """Extract typed knowledge nodes from one session transcript."""
    path = Path(transcript_path)
    if not path.exists():
        return []

    nodes: list[SessionNode] = []
    # tool_use_id -> (tool_name, input) so a failed result can name its cause.
    pending: dict[str, tuple[str, dict]] = {}
    modified_files: set[str] = set()
    error_files: set[str] = set()
    seen_prompts: set[str] = set()

    def add(node_type: NodeType, content: str, **kw) -> None:
        nodes.append(
            SessionNode(
                session_id=session_id,
                node_type=node_type,
                content=_truncate(content),
                **kw,
            )
        )

    for record in _iter_records(path):
        kind = record.get("type")

        if kind == "user":
            content = (record.get("message") or {}).get("content")
            # A plain string is a human prompt; a list is tool results.
            if isinstance(content, str) and content.strip():
                if _is_meaningful_prompt(content) and content.strip() not in seen_prompts:
                    seen_prompts.add(content.strip())
                    add(NodeType.USER_PROMPT, content, importance=0.9)
                continue

            for block in _blocks(record):
                if block.get("type") != "tool_result":
                    continue
                tool_name, tool_input = pending.pop(block.get("tool_use_id", ""), ("", {}))
                if not block.get("is_error"):
                    continue
                target = tool_input.get("file_path") or tool_input.get("command") or ""
                files = [target] if tool_input.get("file_path") else []
                error_files.update(files)
                add(
                    NodeType.ERROR,
                    f"{tool_name} failed on {target}\n{_text_of(block)}",
                    files_involved=files,
                    importance=0.8,
                    metadata={"tool": tool_name},
                )

        elif kind == "assistant":
            for block in _blocks(record):
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name") or ""
                tool_input = block.get("input") or {}
                pending[block.get("id", "")] = (name, tool_input)

                if name in EDIT_TOOLS and tool_input.get("file_path"):
                    file_path = tool_input["file_path"]
                    modified_files.add(file_path)
                    add(
                        NodeType.FILE_MODIFICATION,
                        f"{name} {file_path}",
                        files_involved=[file_path],
                        importance=0.6,
                        metadata={"tool": name},
                    )
                elif name == "Bash":
                    command = tool_input.get("command") or ""
                    for written in _files_written_by_shell(command):
                        modified_files.add(written)
                        add(
                            NodeType.FILE_MODIFICATION,
                            f"shell wrote {written}",
                            files_involved=[written],
                            importance=0.55,
                            metadata={"tool": "Bash"},
                        )
                    if command.startswith("git commit") or " git commit" in command:
                        add(NodeType.COMMIT, command, importance=0.7)
                    elif _is_test_command(command):
                        add(NodeType.TEST_RESULT, command, importance=0.7)

    _mark_unresolved(nodes, modified_files, error_files)
    return nodes


_SHELL_WRITE = re.compile(
    r"""(?:^|[|;&]\s*)          # start of a command
        (?:cat|tee|printf|echo)\s[^|;&]*?>\s*(?P<redirect>[^\s|;&<>]+)
        |
        \bsed\s+-i(?:\s+''|\s+\S+)?\s+[^|;&]*?\s(?P<sed>[^\s|;&]+)$
    """,
    re.VERBOSE | re.MULTILINE,
)


def _files_written_by_shell(command: str) -> set[str]:
    """Files a shell command writes -- redirects and in-place sed.

    An agent told to prefer Bash over the Edit tool still modifies files;
    without this those changes are invisible to memory.
    """
    found: set[str] = set()
    for match in _SHELL_WRITE.finditer(command or ""):
        target = (match.group("redirect") or match.group("sed") or "").strip("\"'")
        if not target or target.startswith(("/dev/", "$", "-")):
            continue
        # Shell metacharacters mean we matched inside a quoted string or an
        # unexpanded variable, not a real path.
        if any(ch in target for ch in '$\\")(`'):
            continue
        # `echo "a -> b"` puts a bare word after what looks like a redirect.
        # Requiring a path shape drops those without losing real targets.
        if "/" not in target and "." not in target:
            continue
        found.add(target)
    return found


def _mark_unresolved(
    nodes: list[SessionNode],
    modified_files: set[str],
    error_files: set[str],
) -> None:
    """Promote errors on files never subsequently modified to unresolved issues.

    This is the dead-end signal. If a file blew up and nothing ever fixed it,
    the next session should be told before it walks into the same wall.
    """
    unfixed = error_files - modified_files
    if not unfixed:
        return
    for node in nodes:
        if node.node_type is not NodeType.ERROR:
            continue
        if any(f in unfixed for f in node.files_involved):
            node.node_type = NodeType.UNRESOLVED_ISSUE
            node.importance = 0.95
            node.tier = MemoryTier.WARM

"""ContextMesh Hook Engine — the single entry point Claude Code calls.

Dispatches on hook_event_name:

  PreToolUse        rewrite Bash through `contextmesh run` to compress output
  UserPromptSubmit  inject recalled memory, once per session
  SessionEnd        harvest the finished transcript into the knowledge graph

Every path is wrapped so a failure exits 0 with no output. A context layer
that can break the user's session is worse than no context layer, so when in
doubt this does nothing at all.
"""

import base64
import json
import os
import sys

# Set CONTEXTMESH_DISABLE=1 to make the hook inert without uninstalling it.
# The benchmark harness uses this as its control arm, so both arms run under
# identical settings, auth, and working directory -- only the interception
# changes. Also a kill switch when the hook misbehaves mid-session.
DISABLE_ENV_VAR = "CONTEXTMESH_DISABLE"

# The two injected blocks are independently switchable. Users may want memory
# without a RepoMap (or the reverse), and the benchmark needs to vary one at a
# time -- measuring them together cannot say which one paid.
NO_REPOMAP_ENV_VAR = "CONTEXTMESH_NO_REPOMAP"
NO_MEMORY_ENV_VAR = "CONTEXTMESH_NO_MEMORY"

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def is_disabled() -> bool:
    return _flag(DISABLE_ENV_VAR)


def repomap_enabled() -> bool:
    return not _flag(NO_REPOMAP_ENV_VAR)


def memory_enabled() -> bool:
    return not _flag(NO_MEMORY_ENV_VAR)


def _db_path() -> str:
    from contextmesh.config import get_config

    return str(get_config().data_dir / "contextmesh.db")


def handle_pre_tool_use(data: dict) -> None:
    """Route Bash through the ContextMesh executor for output compression."""
    if data.get("tool_name") != "Bash":
        return

    command = data.get("tool_input", {}).get("command", "")
    session_id = data.get("session_id", "unknown")

    # Base64 so the shell never has to survive the original quoting.
    cmd_b64 = base64.b64encode(command.encode("utf-8")).decode("utf-8")

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": f"contextmesh run {cmd_b64} --session {session_id}"},
        }
    }))


def handle_user_prompt_submit(data: dict) -> None:
    """Inject recalled memory on the first prompt of a session only.

    Injecting every turn would append a fresh block to the conversation each
    time; the compounding cost would exceed anything recall saves. The
    sessions row is the once-per-session latch.
    """
    session_id = data.get("session_id") or ""
    project_path = data.get("cwd") or os.getcwd()
    if not session_id:
        return

    from contextmesh.memory.store import connect, ensure_session

    db_path = _db_path()
    con = connect(db_path)
    try:
        first_prompt = ensure_session(con, session_id, project_path)
    finally:
        con.close()

    if not first_prompt:
        return

    # Only now pay for the scorer's imports.
    from contextmesh.memory.recall import build_recall_context
    from contextmesh.utils.injector import _build_repomap_from_db

    blocks = []

    # The RepoMap belongs here rather than on the first Bash result, where it
    # used to live. There it never arrived if a session opened with Read or
    # Grep, and when it did arrive the agent had already chosen what to do.
    if repomap_enabled():
        try:
            repomap = _build_repomap_from_db(db_path, project_path)
            if repomap:
                blocks.append(repomap)
        except Exception:
            pass

    if memory_enabled():
        context = build_recall_context(
            db_path=db_path,
            project_path=project_path,
            prompt=data.get("prompt") or "",
            exclude_session=session_id,
        )
        if context:
            blocks.append(context)

    if blocks:
        print("\n\n".join(blocks))


def handle_session_end(data: dict) -> None:
    """Harvest the finished transcript into typed knowledge nodes."""
    session_id = data.get("session_id") or ""
    transcript = data.get("transcript_path") or ""
    project_path = data.get("cwd") or os.getcwd()
    if not (session_id and transcript):
        return

    from contextmesh.memory.extractor import extract_nodes
    from contextmesh.memory.store import save_nodes

    nodes = extract_nodes(transcript, session_id, project_path)
    if nodes:
        save_nodes(_db_path(), session_id, project_path, nodes)


HANDLERS = {
    "PreToolUse": handle_pre_tool_use,
    "UserPromptSubmit": handle_user_prompt_submit,
    "SessionEnd": handle_session_end,
}


def main() -> None:
    if is_disabled():
        sys.exit(0)

    try:
        raw = sys.stdin.read()
        if not raw:
            sys.exit(0)

        data = json.loads(raw)
        # PreToolUse payloads predate hook_event_name; infer it from tool_name.
        event = data.get("hook_event_name") or ("PreToolUse" if data.get("tool_name") else "")
        handler = HANDLERS.get(event)
        if handler:
            handler(data)
    except Exception:
        # Never surface a ContextMesh failure into the user's session.
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()

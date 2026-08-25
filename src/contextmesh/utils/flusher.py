"""
ContextMesh Phase 3: Anti-Context Auto-Flusher

Scans the outbound messages array before it goes to Anthropic.
When the conversation history grows too large, it intelligently
drops old, resolved tool results and file reads — keeping only
the recent "active" turns — and injects a transparent summary note
so Claude always knows exactly what was flushed and why.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
# Total character budget for the whole messages array before flushing kicks in.
# ~200k chars ≈ ~50k tokens. Claude's window is 200k tokens, but we want
# to keep the effective context tight and focused.
FLUSH_TRIGGER_CHARS = 150_000

# How many of the most-recent message pairs (user + assistant) to always keep
# untouched regardless of size. This is the "active window".
KEEP_RECENT_TURNS = 15

# Types of content blocks that are safe to flush (they are ephemeral noise).
# We NEVER flush user prompts, assistant responses, or decisions.
FLUSHABLE_TOOL_NAMES = {
    # File reads
    "read_file", "view_file", "cat", "open",
    # Search / grep
    "grep", "ripgrep", "find", "search", "glob",
    # Directory listing
    "ls", "list_directory", "list_dir",
    # Terminal noise
    "bash", "shell", "run_command", "execute",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _estimate_chars(messages: list) -> int:
    """Roughly count the total characters across all messages."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block.get("content"), str):
                    total += len(block["content"])
                elif isinstance(block.get("text"), str):
                    total += len(block["text"])
    return total


def _is_flushable_tool_result(message: dict) -> tuple[bool, str]:
    """
    Checks if a user message is a tool_result that is safe to flush.
    Returns (is_flushable, tool_name_or_reason).
    """
    if message.get("role") != "user":
        return False, ""

    content = message.get("content", [])
    if not isinstance(content, list):
        return False, ""

    for block in content:
        if block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            inner_content = block.get("content", "")
            if isinstance(inner_content, str) and len(inner_content) > 2000:
                # Large tool result — candidate for flushing
                return True, f"large_tool_result({tool_use_id[:12]})"

    return False, ""


def _is_flushable_assistant_tool_call(message: dict) -> tuple[bool, str]:
    """
    Checks if an assistant message is purely a tool_use call (not a decision/response).
    Returns (is_flushable, tool_name).
    """
    if message.get("role") != "assistant":
        return False, ""

    content = message.get("content", [])
    if not isinstance(content, list):
        return False, ""

    # If the assistant message has text content, it's likely a real response — keep it
    for block in content:
        if block.get("type") == "text" and len(block.get("text", "").strip()) > 50:
            return False, ""

    # Only tool_use blocks = safe to flush
    tool_names = [b.get("name", "") for b in content if b.get("type") == "tool_use"]
    if tool_names:
        # Only flush known noisy tools
        flushable = [t for t in tool_names if t.lower() in FLUSHABLE_TOOL_NAMES]
        if len(flushable) == len(tool_names):  # All calls are flushable noise
            return True, ",".join(tool_names)

    return False, ""


# ── Main Flusher ─────────────────────────────────────────────────────────────

def flush_old_context(payload: dict) -> tuple[dict, int]:
    """
    Main flushing logic. Accepts a parsed payload dict, returns
    (modified_payload, estimated_tokens_saved).
    """
    messages = payload.get("messages", [])
    if not messages:
        return payload, 0

    total_chars = _estimate_chars(messages)

    if total_chars < FLUSH_TRIGGER_CHARS:
        # Context is healthy — nothing to do
        return payload, 0

    logger.warning(
        f"[ContextMesh Flusher] Context bloat detected: ~{total_chars:,} chars "
        f"(~{total_chars // 4:,} tokens). Running anti-context flush..."
    )

    # Split messages into "old" (candidates for flushing) and "recent" (always keep)
    # We always keep at least KEEP_RECENT_TURNS * 2 messages (user+assistant pairs)
    keep_boundary = max(0, len(messages) - (KEEP_RECENT_TURNS * 2))
    old_messages = messages[:keep_boundary]
    recent_messages = messages[keep_boundary:]

    flushed_count = 0
    chars_flushed = 0
    flushed_summaries = []
    kept_old = []

    i = 0
    while i < len(old_messages):
        msg = old_messages[i]

        # Check if this is a flushable assistant→user tool call+result pair
        is_assistant_flush, tool_name = _is_flushable_assistant_tool_call(msg)

        if is_assistant_flush and i + 1 < len(old_messages):
            next_msg = old_messages[i + 1]
            is_result_flush, result_id = _is_flushable_tool_result(next_msg)

            if is_result_flush:
                # Flush both the tool call AND its result
                pair_chars = _estimate_chars([msg, next_msg])
                chars_flushed += pair_chars
                flushed_count += 1
                flushed_summaries.append(f"{tool_name}")
                i += 2  # Skip both
                continue

        # Not a flushable pair — keep it
        kept_old.append(msg)
        i += 1

    if flushed_count == 0:
        logger.info("[ContextMesh Flusher] No flushable turns found in old context.")
        return payload, 0

    tokens_saved = chars_flushed // 4
    tool_list = ", ".join(set(flushed_summaries))

    # Inject a transparent system note so Claude knows what happened
    flush_note = {
        "role": "user",
        "content": (
            f"[ContextMesh Auto-Flush: Removed {flushed_count} old tool call/result pairs "
            f"({chars_flushed:,} chars ≈ {tokens_saved:,} tokens) to keep context focused. "
            f"Tools flushed: {tool_list}. "
            f"Your recent {KEEP_RECENT_TURNS} turns are fully intact. "
            f"Use get_project_architecture() if you need structural context again.]"
        )
    }

    # Rebuild the messages array: kept old + flush note + recent
    new_messages = kept_old + [flush_note] + recent_messages
    payload["messages"] = new_messages

    logger.warning(
        f"[ContextMesh Flusher] Flushed {flushed_count} old tool pairs. "
        f"Saved ~{tokens_saved:,} tokens. "
        f"Messages: {len(messages)} → {len(new_messages)}"
    )

    return payload, tokens_saved

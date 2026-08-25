import json
import logging

logger = logging.getLogger(__name__)

# Character limits for compression
MAX_TOOL_RESULT_LENGTH = 12000
HEAD_LENGTH = 3000
TAIL_LENGTH = 3000

def compress_outbound_payload(body_bytes: bytes) -> tuple[bytes, int]:
    """
    Intercepts the Claude /v1/messages JSON payload and compresses
    massive tool results (like grep or npm test) to save tokens.
    Returns (modified_body_bytes, estimated_tokens_saved).
    """
    try:
        payload = json.loads(body_bytes.decode('utf-8'))
    except Exception:
        # Not JSON or decode error, return as is
        return body_bytes, 0

    if "messages" not in payload:
        return body_bytes, 0

    modified = False
    tokens_saved_estimate = 0

    for message in payload.get("messages", []):
        if message.get("role") == "user" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if block.get("type") == "tool_result" and isinstance(block.get("content"), str):
                    original_content = block["content"]
                    if len(original_content) > MAX_TOOL_RESULT_LENGTH:
                        head = original_content[:HEAD_LENGTH]
                        tail = original_content[-TAIL_LENGTH:]
                        
                        chars_removed = len(original_content) - (HEAD_LENGTH + TAIL_LENGTH)
                        # Rough estimate: ~4 chars per token
                        tokens_saved_estimate += (chars_removed // 4)
                        
                        compression_msg = (
                            f"\n\n... [ContextMesh RTK: Output was too long. "
                            f"{chars_removed} characters compressed from the middle to save context.] ...\n\n"
                        )
                        
                        block["content"] = head + compression_msg + tail
                        modified = True

    if modified:
        logger.warning(f"[ContextMesh RTK] Intercepted outbound payload! Saved ~{tokens_saved_estimate} tokens.")
        return json.dumps(payload).encode('utf-8'), tokens_saved_estimate
    
    return body_bytes, 0

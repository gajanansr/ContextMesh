"""
ContextMesh Smart Proxy.

Sits between Claude Code and the Anthropic API. Every request passes through
three pipeline stages before reaching Anthropic:

  1. Session Resumption Injector  — prepends last-session memory on turn 1
  2. RTK Output Compressor        — crushes massive tool_result noise
  3. Anti-Context Flusher         — drops old resolved turns from long history

On the response side, it sniffs the SSE stream for the Anthropic usage block
and writes a full measurement row to proxy_measurements (no MCP, no sessions
table dependency) so contextmesh stats always shows real data.
"""

import hashlib
import json
import logging
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from contextmesh.config import get_config
from contextmesh.store.db import init_db

logger = logging.getLogger(__name__)

proxy_app = FastAPI(title="ContextMesh Token Proxy")

# Loaded at startup, injected once into the first turn then cleared
_session_preamble: str | None = None


# ── Startup ────────────────────────────────────────────────────────────────────

@proxy_app.on_event("startup")
async def startup_event():
    global _session_preamble
    config = get_config()
    db_path = config.data_dir / "contextmesh.db"
    await init_db(db_path)

    try:
        from contextmesh.utils.resumption import get_last_session_summary
        _session_preamble = await get_last_session_summary(str(db_path))
        if _session_preamble:
            logger.info("[Proxy] Session preamble loaded (%d chars)", len(_session_preamble))
    except Exception as e:
        logger.warning("[Proxy] Could not load session preamble: %s", e)


# ── Session ID extraction ──────────────────────────────────────────────────────

def _extract_session_id(request: Request, payload: dict) -> str:
    """
    Derive a stable session ID without MCP.
    Priority:
      1. x-claude-session-id header (Claude Code sends this)
      2. Hash of first user message content (stable per conversation)
      3. Random fallback
    """
    # Claude Code sometimes sends a session ID header
    for header in ["x-claude-session-id", "x-session-id", "x-request-id"]:
        val = request.headers.get(header)
        if val:
            return val[:64]

    # Hash the first user message for a stable session key
    messages = payload.get("messages", [])
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        content = block.get("text", "")
                        break
            if isinstance(content, str) and content:
                return "proxy_" + hashlib.sha256(content[:200].encode()).hexdigest()[:16]

    return "proxy_" + uuid.uuid4().hex[:16]


# ── Stats writer ───────────────────────────────────────────────────────────────

async def _write_measurement(
    measurement_id: str,
    session_id: str,
    model: str,
    usage: dict,
    original_input_tokens: int,
    rtk_tokens_saved: int,
    flush_tokens_saved: int,
    request_preview: str,
):
    """Write a measurement row to proxy_measurements — the source of truth for stats."""
    try:
        from contextmesh.store.db import get_db
        db = get_db()
        await db.execute(
            """
            INSERT OR IGNORE INTO proxy_measurements (
                measurement_id, session_id, timestamp, model,
                input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens,
                original_input_tokens, rtk_tokens_saved, flush_tokens_saved,
                request_preview
            ) VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                measurement_id,
                session_id,
                model,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_creation_input_tokens", 0),
                usage.get("cache_read_input_tokens", 0),
                original_input_tokens,
                rtk_tokens_saved,
                flush_tokens_saved,
                request_preview,
            ),
        )
        await db.commit()
        logger.info(
            "[Proxy] Wrote measurement %s | session=%s | in=%d | rtk_saved=%d | flush_saved=%d",
            measurement_id[:12], session_id[:16],
            usage.get("input_tokens", 0), rtk_tokens_saved, flush_tokens_saved,
        )
    except Exception as e:
        logger.error("[Proxy] DB write error: %s", e)


async def _sniff_and_record(
    chunk: bytes,
    measurement_id: str,
    session_id: str,
    model: str,
    original_input_tokens: int,
    rtk_tokens_saved: int,
    flush_tokens_saved: int,
    request_preview: str,
):
    """Parse SSE chunk, extract usage block, write measurement."""
    try:
        text = chunk.decode("utf-8", errors="ignore")
        for line in text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            # message_start has the full input usage
            if data.get("type") == "message_start" and "message" in data:
                usage = data["message"].get("usage", {})
                if usage:
                    await _write_measurement(
                        measurement_id, session_id, model, usage,
                        original_input_tokens, rtk_tokens_saved,
                        flush_tokens_saved, request_preview,
                    )
    except Exception as e:
        logger.debug("[Proxy] Sniff error: %s", e)


# ── Main proxy route ───────────────────────────────────────────────────────────

@proxy_app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    url = f"https://api.anthropic.com/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    client = httpx.AsyncClient(timeout=180.0)

    headers = {k.lower(): v for k, v in request.headers.items()}
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers.pop("accept-encoding", None)  # We handle compression ourselves

    raw_body = await request.body()

    # Parse payload once for all pipeline stages
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        payload = {}

    model = payload.get("model", "unknown")
    request_preview = raw_body[:200].decode("utf-8", errors="replace")
    session_id = _extract_session_id(request, payload)
    measurement_id = uuid.uuid4().hex

    # ── Stage 0: Session Resumption Injection (first turn only) ────────────
    global _session_preamble
    if _session_preamble is not None and payload.get("messages"):
        try:
            msgs = payload["messages"]
            if len(msgs) <= 2:
                preamble_msg = {"role": "user", "content": _session_preamble}
                ack_msg = {
                    "role": "assistant",
                    "content": "Understood. I have your session context loaded. What would you like to work on?",
                }
                payload["messages"] = [preamble_msg, ack_msg] + msgs
                logger.info("[Proxy] Injected session preamble")
            _session_preamble = None
        except Exception as e:
            logger.error("[Proxy] Preamble injection error: %s", e)

    # ── Stage 0b: Repomap System Prompt Injection (first turn only) ────────
    try:
        from contextmesh.utils.injector import inject_repomap_into_system_prompt
        config = get_config()
        db_path = str(config.data_dir / "contextmesh.db")
        payload = inject_repomap_into_system_prompt(payload, db_path)
    except Exception as e:
        logger.debug("[Proxy] Repomap injection error: %s", e)

    # Encode after stage 0
    body = json.dumps(payload).encode("utf-8") if payload else raw_body

    # ── Stage 1: RTK Tool Output Compressor ────────────────────────────────
    rtk_tokens_saved = 0
    try:
        from contextmesh.utils.compressor import compress_outbound_payload
        body, rtk_tokens_saved = compress_outbound_payload(body)
    except Exception as e:
        logger.error("[Proxy] RTK compressor error: %s", e)

    # ── Stage 2: Anti-Context Flusher ──────────────────────────────────────
    flush_tokens_saved = 0
    try:
        from contextmesh.utils.flusher import flush_old_context
        parsed = json.loads(body.decode("utf-8"))
        flushed_payload, flush_tokens_saved = flush_old_context(parsed)
        body = json.dumps(flushed_payload).encode("utf-8")
    except Exception as e:
        logger.error("[Proxy] Flusher error: %s", e)

    # Estimate original input tokens (before compression) as chars / 4
    original_chars = len(raw_body)
    compressed_chars = len(body)
    original_input_tokens_est = original_chars // 4

    headers["content-length"] = str(len(body))

    req = client.build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=body,
    )

    try:
        resp = await client.send(req, stream=True)

        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        resp_headers.pop("content-encoding", None)
        resp_headers.pop("content-length", None)
        resp_headers.pop("transfer-encoding", None)
        resp_headers["content-encoding"] = "gzip"

        async def stream_generator():
            import zlib
            compressor = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
            usage_recorded = False
            try:
                async for chunk in resp.aiter_bytes():
                    # Sniff for usage block once per response
                    if not usage_recorded and b'"message_start"' in chunk:
                        await _sniff_and_record(
                            chunk,
                            measurement_id, session_id, model,
                            original_input_tokens_est,
                            rtk_tokens_saved, flush_tokens_saved,
                            request_preview,
                        )
                        usage_recorded = True
                    compressed = compressor.compress(chunk)
                    if compressed:
                        yield compressed
                yield compressor.flush()
            except Exception as e:
                logger.error("[Proxy] Stream error: %s", e)
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=resp.status_code,
            headers=resp_headers,
        )

    except Exception as e:
        logger.error("[Proxy] Request error: %s", e)
        await client.aclose()
        return Response(
            content=json.dumps({
                "type": "error",
                "error": {"type": "api_error", "message": f"ContextMesh Proxy Error: {e}"},
            }),
            status_code=502,
            media_type="application/json",
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(proxy_app, host="127.0.0.1", port=8099)

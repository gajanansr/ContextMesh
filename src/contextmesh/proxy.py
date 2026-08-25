import json
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response
from contextmesh.store.db import init_db
from contextmesh.config import get_config
from contextmesh.utils.resumption import get_last_session_summary

logger = logging.getLogger(__name__)

proxy_app = FastAPI(title='ContextMesh Token Proxy')

_session_preamble: str | None = None

@proxy_app.on_event("startup")
async def startup_event():
    global _session_preamble
    config = get_config()
    db_path = config.data_dir / "contextmesh.db"
    await init_db(db_path)
    _session_preamble = await get_last_session_summary(str(db_path))

async def track_usage(chunk: bytes, saved_tokens: int = 0):
    """Sniff the SSE stream for token usage without breaking the stream."""
    try:
        text = chunk.decode('utf-8', errors='ignore')
        for line in text.splitlines():
            if line.startswith('data: '):
                data = json.loads(line[6:])
                if 'usage' in data and data['type'] in ('message_start', 'message_delta'):
                    usage = data['usage']
                    logger.warning(f"[Proxy] Captured Usage: {usage} | RTK Saved: {saved_tokens}")
                    
                    # Store in DB
                    try:
                        from contextmesh.store.db import get_db
                        import uuid
                        db = get_db()
                        turn_id = f"proxy_{uuid.uuid4().hex[:8]}"
                        
                        routed_tok = usage.get('input_tokens', 0)
                        accum_tok = routed_tok + saved_tokens
                        
                        # Use INSERT OR IGNORE just in case, though turn_id is unique
                        await db.execute(
                            """
                            INSERT INTO token_savings 
                            (turn_id, session_id, timestamp, accumulated_session_tokens, 
                             routed_tokens, mcp_overhead_tokens, hot_tokens, warm_tokens, 
                             cold_tokens, repo_tokens, input_price_per_mtok)
                            VALUES (?, 'proxy_session', datetime('now'), ?, ?, 0, 0, 0, 0, 0, 3.0)
                            """,
                            (turn_id, accum_tok, routed_tok)
                        )
                        await db.commit()
                    except Exception as db_e:
                        logger.error(f"DB Error: {db_e}")
    except Exception:
        pass


@proxy_app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
async def proxy(path: str, request: Request):
    url = f"https://api.anthropic.com/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
        
    client = httpx.AsyncClient(timeout=180.0)
    
    headers = {k.lower(): v for k, v in request.headers.items()}
    headers.pop('host', None)
    headers.pop('content-length', None)
    
    # Crucial: Remove accept-encoding to prevent double-compression issues
    # and to ensure we can sniff the raw JSON chunks for token counts
    headers.pop('accept-encoding', None)
    
    raw_body = await request.body()
    
    # --- CONTEXTMESH RESUMPTION INJECTION ---
    global _session_preamble
    if _session_preamble is not None:
        try:
            payload = json.loads(raw_body.decode('utf-8'))
            if "messages" in payload and isinstance(payload["messages"], list):
                if len(payload["messages"]) <= 2:
                    # Inject before the first real user message
                    preamble_msg = {"role": "user", "content": _session_preamble}
                    ack_msg = {"role": "assistant", "content": "Understood. I have your session context loaded. What would you like to work on?"}
                    
                    payload["messages"] = [preamble_msg, ack_msg] + payload["messages"]
                    raw_body = json.dumps(payload).encode('utf-8')
            
            _session_preamble = None
        except Exception as e:
            logger.error(f"Error injecting session preamble: {e}")
    
    # --- CONTEXTMESH RTK: Compress outbound payload ---
    from contextmesh.utils.compressor import compress_outbound_payload
    from contextmesh.utils.flusher import flush_old_context
    body, rtk_tokens_saved = compress_outbound_payload(raw_body)

    # --- CONTEXTMESH PHASE 3: Anti-Context Auto-Flusher ---
    try:
        parsed = json.loads(body.decode('utf-8'))
        flushed_payload, flush_tokens_saved = flush_old_context(parsed)
        flushed_body = json.dumps(flushed_payload).encode('utf-8')
        saved_tokens = rtk_tokens_saved + flush_tokens_saved
        body = flushed_body
    except Exception as flush_err:
        logger.error(f"[Flusher] Error during context flush: {flush_err}")
        saved_tokens = rtk_tokens_saved

    # Update content-length if we changed the body
    if len(body) != len(raw_body):
        headers['content-length'] = str(len(body))
    
    req = client.build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=body
    )
    
    try:
        # Use stream=True to support Claude's real-time streaming
        resp = await client.send(req, stream=True)
        
        # Strip out any headers that would confuse Claude Code
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        resp_headers.pop('content-encoding', None)
        resp_headers.pop('content-length', None)
        resp_headers.pop('transfer-encoding', None)
        
        # Set content-encoding to gzip so Claude Code correctly decompresses it
        resp_headers['content-encoding'] = 'gzip'
        
        async def stream_generator():
            import zlib
            compressor = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
            try:
                async for chunk in resp.aiter_bytes():
                    if b'"usage"' in chunk:
                        await track_usage(chunk, saved_tokens)
                    compressed = compressor.compress(chunk)
                    if compressed:
                        yield compressed
                yield compressor.flush()
            except Exception as e:
                logger.error(f"Stream error: {e}")
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=resp.status_code,
            headers=resp_headers
        )
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        await client.aclose()
        error_json = {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": f"ContextMesh Proxy Error: {str(e)}"
            }
        }
        return Response(content=json.dumps(error_json), status_code=502, media_type="application/json")

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(proxy_app, host='127.0.0.1', port=8099)

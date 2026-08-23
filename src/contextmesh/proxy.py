import json
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response
from contextmesh.store.db import init_db
from contextmesh.config import get_config

logger = logging.getLogger(__name__)

proxy_app = FastAPI(title='ContextMesh Token Proxy')

@proxy_app.on_event("startup")
async def startup_event():
    config = get_config()
    db_path = config.data_dir / "contextmesh.db"
    await init_db(db_path)

async def track_usage(chunk: bytes):
    """Sniff the SSE stream for token usage without breaking the stream."""
    try:
        text = chunk.decode('utf-8', errors='ignore')
        for line in text.splitlines():
            if line.startswith('data: '):
                data = json.loads(line[6:])
                if 'usage' in data and data['type'] in ('message_start', 'message_delta'):
                    usage = data['usage']
                    logger.warning(f"[Proxy] Captured Usage: {usage}")
                    
                    # Store in DB
                    try:
                        from contextmesh.store.db import get_db
                        import uuid
                        db = get_db()
                        turn_id = f"proxy_{uuid.uuid4().hex[:8]}"
                        # We don't have the session_id or accumulated tokens from just the proxy
                        # But we can log it with a dummy session ID for global reporting
                        # or infer from process? For now just log it under a 'proxy_session'
                        # In a true integration, we'd extract x-claude-session-id or correlate
                        # by timestamp.
                        # For now, let's just insert a simple row so stats aren't 0.
                        input_tok = usage.get('input_tokens', 0)
                        out_tok = usage.get('output_tokens', 0)
                        await db.execute(
                            """
                            INSERT INTO token_savings 
                            (turn_id, session_id, timestamp, accumulated_session_tokens, 
                             routed_tokens, mcp_overhead_tokens, hot_tokens, warm_tokens, 
                             cold_tokens, repo_tokens, input_price_per_mtok)
                            VALUES (?, 'proxy_session', datetime('now'), ?, ?, 0, 0, 0, 0, 0, 3.0)
                            """,
                            (turn_id, input_tok, input_tok)
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
    
    body = await request.body()
    
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
                        await track_usage(chunk)
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

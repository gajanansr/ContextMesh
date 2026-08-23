import json
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response

logger = logging.getLogger(__name__)

proxy_app = FastAPI(title='ContextMesh Token Proxy')

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
                    # In a full implementation, we'd write this to SQLite here
    except Exception:
        pass


@proxy_app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
async def proxy(path: str, request: Request):
    url = f"https://api.anthropic.com/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
        
    client = httpx.AsyncClient(timeout=180.0)
    
    headers = dict(request.headers)
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
        resp_headers = dict(resp.headers)
        resp_headers.pop('content-encoding', None)
        resp_headers.pop('content-length', None)
        
        async def stream_generator():
            try:
                async for chunk in resp.aiter_bytes():
                    # Sniff the chunk for token usage stats
                    if b'"usage"' in chunk:
                        await track_usage(chunk)
                    yield chunk
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
        return Response(content=f"Bad Gateway: {str(e)}", status_code=502)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(proxy_app, host='127.0.0.1', port=8099)

import uuid
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response, BackgroundTasks
import httpx

proxy_app = FastAPI(title='ContextMesh Token Proxy')

class TokenProxy:
    async def proxy_request(self, request: Request) -> Response:
        url = request.url.path
        if request.url.query:
            url += f"?{request.url.query}"
            
        client = httpx.AsyncClient(base_url="https://api.anthropic.com")
        
        headers = dict(request.headers)
        headers.pop('host', None)
        headers.pop('content-length', None)
        
        body = await request.body()
        
        try:
            req = client.build_request(
                method=request.method,
                url=url,
                headers=headers,
                content=body
            )
            resp = await client.send(req, stream=True)
            
            # Streaming the response back and recording tokens
            # For simplicity in this implementation, we buffer the full response
            # A true streaming proxy would yield chunks while parsing SSE
            content = await resp.aread()
            
            # Simple heuristic for JSON vs SSE
            try:
                data = json.loads(content)
                if 'usage' in data:
                    usage = data['usage']
                    input_toks = usage.get('input_tokens', 0)
                    output_toks = usage.get('output_tokens', 0)
                    cache_read = usage.get('cache_read_input_tokens', 0)
                    cache_create = usage.get('cache_creation_input_tokens', 0)
                    
                    # Store in DB (mocked or background task)
                    print(f"[Proxy] Usage: {input_toks} in, {output_toks} out")
            except:
                pass
            
            return Response(content=content, status_code=resp.status_code, headers=dict(resp.headers))
            
        except Exception as e:
            print(f"Proxy error: {e}")
            return Response(content=str(e), status_code=502)

    async def get_stats(self) -> dict:
        return {"status": "ok", "message": "Stats endpoint"}

proxy_handler = TokenProxy()

@proxy_app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
async def proxy(path: str, request: Request):
    if path == "proxy/stats":
        return await proxy_handler.get_stats()
    return await proxy_handler.proxy_request(request)

@proxy_app.get('/proxy/stats')
async def stats():
    return await proxy_handler.get_stats()

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(proxy_app, host='127.0.0.1', port=8099)

import httpx
import asyncio

async def test():
    async with httpx.AsyncClient() as client:
        req = client.build_request("POST", "https://api.anthropic.com/v1/messages", headers={"x-api-key": "fake", "anthropic-version": "2023-06-01"}, json={"model": "claude-3-haiku-20240307", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10})
        resp = await client.send(req, stream=True)
        headers = {k.lower(): v for k, v in resp.headers.items()}
        print("Before pop:", headers)
        headers.pop('content-encoding', None)
        headers.pop('content-length', None)
        print("After pop:", headers)

asyncio.run(test())

"""Test-only HTTP/WS port forward. No hardware, credentials or payload logging."""
import asyncio
from aiohttp import ClientSession, DummyCookieJar, WSMsgType, web
from server.bench_server import create_app


class LocalForward:
    async def start(self, tokens):
        self.session = ClientSession(cookie_jar=DummyCookieJar(), auto_decompress=False)
        proxy = web.Application(client_max_size=16 * 1024 * 1024)
        proxy.router.add_route('*', '/{path:.*}', self.forward)
        self.proxy_runner = web.AppRunner(proxy, access_log=None, shutdown_timeout=1)
        await self.proxy_runner.setup()
        site = web.TCPSite(self.proxy_runner, '127.0.0.1', 0)
        await site.start()
        self.url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        self.backend_runner = web.AppRunner(create_app(tokens, origin=self.url, ui_forward_peers=['127.0.0.1/32']), access_log=None, shutdown_timeout=1)
        await self.backend_runner.setup()
        backend = web.TCPSite(self.backend_runner, '127.0.0.1', 0)
        await backend.start()
        self.target = 'http://127.0.0.1:' + str(backend._server.sockets[0].getsockname()[1])
        self.http_forwarded = self.ws_forwarded = False
        return self

    async def forward(self, request):
        # Deliberately rewrite Host to the backend address and add proxy headers.
        headers = {key: value for key, value in request.headers.items() if key.lower() not in
                   {'host', 'connection', 'upgrade', 'content-length', 'transfer-encoding', 'sec-websocket-key', 'sec-websocket-version', 'sec-websocket-extensions'}}
        headers.update({'X-Forwarded-For': request.remote, 'X-Forwarded-Host': request.host,
                        'X-Forwarded-Proto': 'http', 'Forwarded': f'for={request.remote};host="{request.host}";proto=http'})
        target = self.target + str(request.rel_url)
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            self.ws_forwarded = True
            upstream = await self.session.ws_connect(target, headers=headers)
            downstream = web.WebSocketResponse()
            await downstream.prepare(request)

            async def relay(source, dest):
                async for frame in source:
                    if frame.type == WSMsgType.TEXT: await dest.send_str(frame.data)
                    elif frame.type == WSMsgType.BINARY: await dest.send_bytes(frame.data)
                    else: break
            tasks = [asyncio.create_task(relay(downstream, upstream)), asyncio.create_task(relay(upstream, downstream))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await upstream.close(); await downstream.close()
            return downstream
        self.http_forwarded = True
        async with self.session.request(request.method, target, headers=headers, data=await request.read(), allow_redirects=False) as response:
            result = web.StreamResponse(status=response.status, headers={key: value for key, value in response.headers.items()
                if key.lower() not in {'content-length', 'transfer-encoding', 'connection'}})
            await result.prepare(request)
            try:
                async for chunk in response.content.iter_chunked(65536): await result.write(chunk)
                await result.write_eof()
            except ConnectionError:
                pass
            return result

    async def close(self):
        await self.session.close()
        await self.proxy_runner.cleanup()
        await self.backend_runner.cleanup()

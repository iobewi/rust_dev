import asyncio
import json
from aiohttp import web
from test_http import TOKENS
from server.bench_server import create_app

async def main():
    tokens={**TOKENS,'agent-second-12345678':{'id':'agent-second','role':'agent','devices':['board-b']}}
    runner=web.AppRunner(create_app(tokens),access_log=None)
    await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    print(json.dumps({'url':'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])}),flush=True)
    try:await asyncio.Event().wait()
    finally:await runner.cleanup()

asyncio.run(main())

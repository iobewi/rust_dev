import asyncio
import json
import sys
from aiohttp import web
from test_http import TOKENS
from server.bench_server import create_app
from local_forward_fixture import LocalForward

async def main():
    tokens={**TOKENS,'agent-second-12345678':{'id':'agent-second','role':'agent','devices':['board-b']}}
    if '--forwarded' in sys.argv:
        forward = await LocalForward().start(tokens)
        print(json.dumps({'url': forward.url}), flush=True)
        try: await asyncio.Event().wait()
        finally: await forward.close()
        return
    runner=web.AppRunner(create_app(tokens),access_log=None)
    await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    print(json.dumps({'url':'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])}),flush=True)
    try:await asyncio.Event().wait()
    finally:await runner.cleanup()

asyncio.run(main())

"""Local test fixture. Uses only a RAM-backed agent, never browser hardware."""
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aiohttp import web
from server.bench_server import create_app
from tools.sim_agent import run

TOKENS = {
    "pytest-client-test-token": {"id": "pytest", "role": "client", "scopes": ["read", "control", "program"]},
    "pytest-reader-test-token": {"id": "reader", "role": "client", "scopes": ["read"]},
    "pytest-agent-test-token": {"id": "agent", "role": "agent", "devices": ["board"]},
}


async def main():
    runner = web.AppRunner(create_app(TOKENS), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    print(json.dumps({"url": url}), flush=True)
    task = asyncio.create_task(run(url, "pytest-agent-test-token", "board", grant=0))
    try:
        await asyncio.Event().wait()
    finally:
        task.cancel()
        await runner.cleanup()


asyncio.run(main())

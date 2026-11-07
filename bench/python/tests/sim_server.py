"""Local test fixture. Uses only a RAM-backed agent, never browser hardware."""
import asyncio
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aiohttp import web
from server.bench_server import create_app
from tools.sim_agent import run

TOKENS = {
    "pytest-client-test-token": {"id": "pytest", "role": "client", "scopes": ["read", "control", "program"]},
    "pytest-second-test-token": {"id": "second", "role": "client", "scopes": ["read", "control"]},
    "pytest-reader-test-token": {"id": "reader", "role": "client", "scopes": ["read"]},
    "pytest-agent-test-token": {"id": "agent", "role": "agent", "devices": ["board"]},
    "pytest-agent2-test-token": {"id": "agent2", "role": "agent", "devices": ["board2"]},
}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--grant", type=int, default=0)
    parser.add_argument("--chip", default="esp32s3", help="Chip family the RAM simulator reports (simulation only)")
    parser.add_argument("--second", action="store_true", help="Also register a second simulated card 'board2' (same family as --chip)")
    parser.add_argument("--script", default="[]", help="JSON [[delay_s, grant_seconds], ...] simulated operator gestures")
    args = parser.parse_args()
    runner = web.AppRunner(create_app(TOKENS), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    print(json.dumps({"url": url}), flush=True)
    task = asyncio.create_task(run(url, "pytest-agent-test-token", "board", grant=args.grant, script=json.loads(args.script), chip=args.chip))
    second = (asyncio.create_task(run(url, "pytest-agent2-test-token", "board2", chip=args.chip)) if args.second else None)
    try:
        await asyncio.Event().wait()
    finally:
        task.cancel()
        if second:
            second.cancel()
        await runner.cleanup()


asyncio.run(main())

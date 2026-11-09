"""Local test fixture. Uses only a RAM-backed agent, never browser hardware."""
import asyncio
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aiohttp import ClientSession, web
from server.bench_server import create_app
from tools.sim_agent import run

TOKENS = {
    "pytest-client-test-token": {"id": "pytest", "role": "client", "scopes": ["read", "control", "program"]},
    "pytest-second-test-token": {"id": "second", "role": "client", "scopes": ["read", "control"]},
    "pytest-reader-test-token": {"id": "reader", "role": "client", "scopes": ["read"]},
    "pytest-agent-test-token": {"id": "agent", "role": "agent", "devices": ["board"]},
    "pytest-agent2-test-token": {"id": "agent2", "role": "agent", "devices": ["board2"]},
}


async def wait_for_reservation(url, event):
    """Set `event` when the card 'board' first shows a reservation (read-only polling of the public device list)."""
    async with ClientSession(headers={"Authorization": "Bearer pytest-client-test-token"}) as session:
        while not event.is_set():
            try:
                async with session.get(url + "/api/v1/devices") as response:
                    devices = (await response.json()).get("devices", [])
                if any(d.get("id") == "board" and d.get("reservation") for d in devices):
                    event.set()
                    return
            except Exception:                                  # the server is still starting: try again
                pass
            await asyncio.sleep(0.02)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--grant", type=int, default=0)
    parser.add_argument("--chip", default="esp32s3", help="Chip family the RAM simulator reports (simulation only)")
    parser.add_argument("--second", action="store_true", help="Also register a second simulated card 'board2' (same family as --chip)")
    parser.add_argument("--script-start", choices=["connect", "reservation"], default="connect",
                        help="When the simulated operator's script clock starts: at the agent's connection (default) or when the card "
                             "'board' first shows a reservation (the recipe under test has started and reserved it): an explicit state signal")
    parser.add_argument("--script", default="[]", help="JSON [[delay_s, grant_seconds], ...] simulated operator gestures")
    args = parser.parse_args()
    runner = web.AppRunner(create_app(TOKENS), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    print(json.dumps({"url": url}), flush=True)
    started = asyncio.Event() if args.script_start == "reservation" else None
    watcher = asyncio.create_task(wait_for_reservation(url, started)) if started is not None else None
    task = asyncio.create_task(run(url, "pytest-agent-test-token", "board", grant=args.grant, script=json.loads(args.script), chip=args.chip,
                                   script_start=started))
    second = (asyncio.create_task(run(url, "pytest-agent2-test-token", "board2", chip=args.chip)) if args.second else None)
    try:
        await asyncio.Event().wait()
    finally:
        task.cancel()
        if watcher:
            watcher.cancel()
        if second:
            second.cancel()
        await runner.cleanup()


asyncio.run(main())

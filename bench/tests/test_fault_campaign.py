"""Fault-campaign boundaries over real loopback HTTP/WS/SSE, no USB hardware."""
import asyncio
import base64
from collections import deque
import hashlib
import json
from pathlib import Path
import sys
import unittest

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python/src"))
from server.bench_server import BROKER, create_app
from iobewi_bench import BenchClient, Board, ContinuityError

CLIENT = {"Authorization": "Bearer campaign-client-test-token"}
TOKENS = {
    "campaign-client-test-token": {"id": "campaign", "role": "client", "scopes": ["read", "control"]},
    **{f"campaign-agent-{name}-token": {"id": name, "role": "agent", "devices": [name]}
       for name in ("board-a", "board-b")},
}


class FaultCampaignTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(TOKENS)))
        await self.client.start_server()
        self.sockets = []

    async def asyncTearDown(self):
        for socket in self.sockets:
            await socket.close()
        await self.client.close()

    async def agent(self, name):
        socket = await self.client.ws_connect("/agent/v1/ws")
        self.sockets.append(socket)
        await socket.send_json({"type": "auth", "version": 1, "token": f"campaign-agent-{name}-token"})
        welcome = await socket.receive_json(timeout=2)
        self.assertEqual(welcome["type"], "welcome")
        await socket.send_json({"type": "register", "device": {"id": name, "chip": "esp32s3",
            "flash_size": 4194304, "capabilities": ["identify", "reset", "flash.read"]}})
        registered = await socket.receive_json(timeout=2)
        self.assertEqual(registered["type"], "registered")
        return socket, registered["generation"]

    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, path, headers=CLIENT, **kwargs)
        self.assertLess(response.status, 300)
        return await response.json()

    async def lease(self, name):
        return await self.request("POST", "/api/v1/leases", json={"device": name, "ttl": 60})

    async def wait_device(self, name, predicate):
        for _ in range(200):
            devices = (await self.request("GET", "/api/v1/devices"))["devices"]
            device = next(d for d in devices if d["id"] == name)
            if predicate(device):
                return device
            await asyncio.sleep(.01)
        self.fail("Device state did not reach the expected boundary")

    async def test_same_family_routes_to_the_reserved_agent_and_isolates_disconnect(self):
        a, gen_a = await self.agent("board-a")
        b, _ = await self.agent("board-b")
        lease_a, lease_b = await self.lease("board-a"), await self.lease("board-b")
        op_a = await self.request("POST", "/api/v1/operations", json={"lease": lease_a["id"], "command": "identify"})
        op_b = await self.request("POST", "/api/v1/operations", json={"lease": lease_b["id"], "command": "identify"})
        offer_a, offer_b = await a.receive_json(timeout=2), await b.receive_json(timeout=2)
        self.assertEqual((offer_a["id"], offer_a["device"]), (op_a["id"], "board-a"))
        self.assertEqual((offer_b["id"], offer_b["device"]), (op_b["id"], "board-b"))
        # A different agent must not execute A's pending operation.
        await b.send_json({"type": "ready", "id": op_a["id"]})
        refused = await b.receive_json(timeout=2)
        self.assertEqual((refused["type"], refused["error"]["code"]), ("error", "stale_agent"))
        await b.close()
        await self.wait_device("board-b", lambda d: not d["connected"])
        own = await self.wait_device("board-a", lambda d: d["connected"])
        self.assertEqual(own["generation"], gen_a)
        self.assertEqual(own["reservation"]["owner"], "campaign")
        self.assertFalse(own["quarantined"])
        await a.send_json({"type": "ready", "id": op_a["id"]})
        self.assertEqual((await a.receive_json(timeout=2))["type"], "execute")
        await a.send_json({"type": "result", "id": op_a["id"], "state": "completed", "result": {"target": "board-a"}})
        self.assertEqual((await a.receive_json(timeout=2))["type"], "result_ack")
        done_a = await self.request("GET", "/api/v1/operations/" + op_a["id"])
        done_b = await self.request("GET", "/api/v1/operations/" + op_b["id"])
        self.assertEqual(done_a["result"], {"target": "board-a"})
        self.assertEqual(done_b["state"], "cancelled")

    async def test_sse_three_mib_reconnects_at_next_cursor_without_skipping_or_duplicate_bytes(self):
        socket, generation = await self.agent("board-a")
        chunk = bytes(range(256)) * 48
        count = 256  # 3 MiB, many batches beyond the 48 KiB SSE batch bound.
        for sequence in range(1, count + 1):
            await socket.send_json({"type": "uart", "device": "board-a", "generation": generation,
                                    "seq": sequence, "data": base64.b64encode(chunk).decode()})
        await self.wait_device("board-a", lambda d: d["seq"] == count + 1)
        output, sources, cursor, epoch = bytearray(), [], 0, None
        # Deliberately close the first subscriber halfway and open an independent
        # stream at `next`, never `latest` (which can skip queued batches).
        async with asyncio.timeout(20):
            for target in (len(chunk) * (count // 2), len(chunk) * count):
                stream = await self.client.get(f"/api/v1/devices/board-a/events/stream?after={cursor}", headers=CLIENT)
                self.assertEqual(stream.status, 200)
                try:
                    while len(output) < target:
                        line = await stream.content.readline()
                        if not line.startswith(b"data: "):
                            continue
                        batch = json.loads(line[6:])
                        self.assertEqual(batch["lost"], 0)
                        epoch = epoch or batch["epoch"]
                        self.assertEqual(batch["epoch"], epoch)
                        self.assertGreaterEqual(batch["next"], cursor)
                        for event in batch["events"]:
                            self.assertGreater(event["seq"], cursor)
                            if event["kind"] == "uart":
                                self.assertEqual(event["source_lost"], 0)
                                sources.append(event["source_seq"])
                                output.extend(base64.b64decode(event["data"], validate=True))
                        cursor = batch["next"]
                finally:
                    stream.close()
        self.assertEqual(sources, list(range(1, count + 1)))
        self.assertEqual(len(output), 3 * 1024 * 1024)
        self.assertEqual(hashlib.sha256(output).digest(), hashlib.sha256(chunk * count).digest())

    async def test_sse_reports_both_ring_eviction_and_source_gap_on_resume(self):
        socket, generation = await self.agent("board-a")
        broker = self.client.server.app[BROKER]
        broker.devices["board-a"]["events"] = deque(broker.devices["board-a"]["events"], maxlen=4)
        for sequence in (1, 3, 4, 5, 6):
            await socket.send_json({"type": "uart", "device": "board-a", "generation": generation,
                                    "seq": sequence, "data": "AA=="})
        await self.wait_device("board-a", lambda d: d["seq"] == 6)
        stream = await self.client.get("/api/v1/devices/board-a/events/stream?after=0", headers=CLIENT)
        try:
            batch = json.loads((await asyncio.wait_for(stream.content.readline(), 2))[6:])
        finally:
            stream.close()
        self.assertEqual(batch["lost"], 2)
        self.assertEqual(batch["events"][0]["source_lost"], 1)
        self.assertEqual(batch["next"], 6)

    async def test_server_restart_at_same_origin_rejects_old_board_before_any_post(self):
        _, generation = await self.agent("board-a")
        lease = await self.lease("board-a")
        health = await self.request("GET", "/health")
        origin = str(self.client.make_url("")).rstrip("/")
        client = BenchClient(origin, "campaign-client-test-token")
        old = Board(client, {"id": "board-a", "generation": generation}, health["epoch"], lease["id"], False, None)
        port = self.client.server.port
        await self.client.close()
        self.client = TestClient(TestServer(create_app(TOKENS), port=port))
        await self.client.start_server()
        self.assertEqual(str(self.client.make_url("")).rstrip("/"), origin)
        self.assertNotEqual((await self.request("GET", "/health"))["epoch"], health["epoch"])
        with self.assertRaises(ContinuityError):
            await asyncio.to_thread(old.identify)
        self.assertEqual(self.client.server.app[BROKER].operations, {})

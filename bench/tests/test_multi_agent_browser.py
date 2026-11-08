"""Multi-agent browser isolation tests; no physical hardware involved."""
import asyncio
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from server.bench_server import create_app, BROKER, SESSIONS
from test_http import TOKENS, ALICE


class MultiAgentBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tokens = dict(TOKENS)
        self.tokens['multi-agent-secret-123456'] = {
            'id': 'multi-agent', 'role': 'agent', 'devices': ['board-b']}
        self.app = create_app(self.tokens)
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.headers = {'Origin': str(self.client.make_url('/')).rstrip('/')}
        response = await self.client.post('/session/v1/open', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.session = await response.json()
        self.sockets = []

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.client.close()

    async def connect(self, device, register=True):
        ws = await self.client.ws_connect('/agent/v1/ws', headers=self.headers)
        self.sockets.append(ws)
        await ws.send_json({'type': 'auth', 'version': 1, 'device': device})
        if device not in self.session['devices']:
            frame = await ws.receive(timeout=2)
            self.assertNotEqual(frame.type.name, 'TEXT')
            return ws, None
        reply = await ws.receive_json()
        self.assertEqual(reply['type'], 'welcome')
        if not register:
            return ws, None
        await ws.send_json({'type': 'register', 'device': {
            'id': device, 'chip': 'esp32s3', 'flash_size': 4194304,
            'capabilities': ['identify', 'flash.read']}})
        reply = await ws.receive_json()
        self.assertEqual(reply['type'], 'registered')
        return ws, reply['generation']

    async def test_simultaneous_agents_isolated_registration_and_disconnect(self):
        a, ga = await self.connect('board')
        b, gb = await self.connect('board-b')
        broker = self.app[BROKER]
        self.assertNotEqual(broker.devices['board']['agent'], broker.devices['board-b']['agent'])
        self.assertNotEqual(ga, gb)
        await a.send_json({'type': 'grant', 'device': 'board-b', 'generation': gb, 'seconds': 60})
        reply = await a.receive_json()
        self.assertEqual(reply['error']['code'], 'stale_agent')
        self.assertEqual(broker.devices['board-b']['grant_until'], 0)
        await a.close()
        for _ in range(20):
            if not broker.devices['board']['connected']:
                break
            await asyncio.sleep(.01)
        self.assertFalse(broker.devices['board']['connected'])
        self.assertTrue(broker.devices['board-b']['connected'])
        self.assertEqual(broker.devices['board-b']['generation'], gb)

    async def test_wrong_board_and_duplicate_agent_are_refused(self):
        a, _ = await self.connect('board')
        b, _ = await self.connect('board', register=False)
        await b.send_json({'type': 'register', 'device': {
            'id': 'board-b', 'chip': 'esp32s3', 'flash_size': 4194304,
            'capabilities': ['identify']}})
        frame = await b.receive(timeout=2)
        self.assertNotEqual(frame.type.name, 'TEXT')
        self.assertTrue(self.app[BROKER].devices['board']['connected'])
        await a.send_json({'type': 'register', 'device': {
            'id': 'board-b', 'chip': 'esp32s3', 'flash_size': 4194304,
            'capabilities': ['identify']}})
        reply = await a.receive_json()
        self.assertEqual(reply['error']['code'], 'device_forbidden')

    async def test_invalid_device_does_not_register(self):
        await self.connect('not-allowed')
        self.assertFalse(self.app[BROKER].agents)

    async def test_local_manual_targets_own_agent(self):
        a, ga = await self.connect('board')
        b, gb = await self.connect('board-b')
        for device, generation, ws in [('board', ga, a), ('board-b', gb, b)]:
            response = await self.client.post('/api/v1/manual-operations', headers=self.headers,
                json={'device': device, 'generation': generation, 'command': 'identify'})
            self.assertEqual(response.status, 202)
            op = await response.json()
            self.assertTrue(op['local_control'])
            offered = await ws.receive_json(timeout=2)
            self.assertEqual(offered['id'], op['id'])
            response = await self.client.delete('/api/v1/operations/' + op['id'], headers=self.headers)
            self.assertEqual(response.status, 200)

    async def test_artifact_owner_selected_by_bound_device(self):
        await self.connect('board')
        await self.connect('board-b')
        data = b'browser multi-agent proof'
        digest = hashlib.sha256(data).hexdigest()
        for device in ('board', 'board-b'):
            headers = {**self.headers, 'X-SHA256': digest,
                       'X-Bench-Artifact-Role': 'agent', 'X-Bench-Device': device}
            response = await self.client.post('/api/v1/artifacts', headers=headers, data=data)
            self.assertEqual(response.status, 201)
            artifact = await response.json()
            expected_agent = self.app[BROKER].devices[device]['agent']
            self.assertEqual(self.app[BROKER].artifacts[artifact['id']]['owner'], 'agent:' + expected_agent)
        response = await self.client.post('/api/v1/artifacts', headers={
            **self.headers, 'X-SHA256': digest,
            'X-Bench-Artifact-Role': 'agent', 'X-Bench-Device': 'forbidden'}, data=data)
        self.assertEqual(response.status, 403)

    async def test_other_browser_session_cannot_claim_agent_identity(self):
        await self.connect('board')
        other = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await other.start_server()
        try:
            origin = str(other.make_url('/')).rstrip('/')
            r = await other.post('/session/v1/open', headers={'Origin': origin})
            self.assertEqual(r.status, 200)
            ws = await other.ws_connect('/agent/v1/ws', headers={'Origin': origin})
            await ws.send_json({'type': 'auth', 'version': 1, 'device': 'board'})
            self.assertEqual((await ws.receive_json())['type'], 'welcome')
            await ws.send_json({'type': 'register', 'device': {
                'id': 'board', 'chip': 'esp32s3', 'flash_size': 4194304,
                'capabilities': ['identify']}})
            self.assertEqual((await ws.receive_json())['error']['code'], 'device_connected')
            await ws.close()
            self.assertTrue(self.app[BROKER].devices['board']['connected'])
        finally:
            await other.close()

    async def test_expiration_closes_all_browser_agents(self):
        await self.connect('board')
        await self.connect('board-b')
        for session in self.app[SESSIONS].entries.values():
            session['until'] = 0
        for _ in range(50):
            if not self.app[BROKER].agents:
                break
            await asyncio.sleep(.05)
        self.assertFalse(self.app[BROKER].agents)

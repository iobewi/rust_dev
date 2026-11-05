import asyncio
import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from server.bench_server import create_app, SESSIONS, BROKER
from server.ui_sessions import local_request
from protocol import Fault
from test_http import TOKENS, ALICE, BOB

class BrowserSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(TOKENS)
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.origin = str(self.client.make_url('/')).rstrip('/')
        self.headers = {'Origin': self.origin}
        self.ws = None

    async def asyncTearDown(self):
        if self.ws is not None: await self.ws.close()
        await self.client.close()

    async def open(self):
        response = await self.client.post('/session/v1/open', headers=self.headers)
        self.assertEqual(response.status, 200)
        return response, await response.json()

    async def agent(self):
        await self.open()
        self.ws = await self.client.ws_connect('/agent/v1/ws', headers=self.headers)
        await self.ws.send_json({'type': 'auth', 'version': 1})
        self.assertEqual((await self.ws.receive_json())['type'], 'welcome')
        await self.ws.send_json({'type': 'register', 'device': {'id': 'board', 'chip': 'esp32s3', 'flash_size': 4194304,
            'capabilities': ['identify', 'reset', 'flash.erase', 'flash.read']}})
        self.generation = (await self.ws.receive_json())['generation']

    async def test_client_only_config_needs_no_agent_token_for_the_local_page(self):
        client = TestClient(TestServer(create_app({key: value for key, value in TOKENS.items() if value['role'] == 'client'})), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            headers = {'Origin': str(client.make_url('/')).rstrip('/')}
            r = await client.post('/session/v1/open', headers=headers)
            self.assertEqual((await r.json())['devices'], ['esp32-local'])
            ws = await client.ws_connect('/agent/v1/ws', headers=headers)
            await ws.send_json({'type': 'auth', 'version': 1})
            self.assertEqual((await ws.receive_json())['type'], 'welcome')
            await ws.send_json({'type': 'register', 'device': {'id': 'esp32-local', 'chip': 'esp32s3', 'flash_size': 4194304, 'capabilities': ['identify']}})
            self.assertEqual((await ws.receive_json())['type'], 'registered')
            await ws.close()
        finally:
            await client.close()

    async def test_cookie_is_http_only_and_external_bearer_credentials_remain_independent(self):
        response, session = await self.open()
        cookie = response.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie); self.assertIn('SameSite=Strict', cookie)
        self.assertNotIn('token', session); self.assertNotIn('agent', session)
        r = await self.client.get('/api/v1/devices'); self.assertEqual(r.status, 200)
        r = await self.client.get('/api/v1/devices', headers={'Authorization': 'Bearer invalid-invalid-invalid'})
        self.assertEqual(r.status, 401)
        r = await self.client.get('/api/v1/devices', headers=ALICE); self.assertEqual(r.status, 200)
        _, again = await self.open(); self.assertEqual(again['client'], session['client'])

    async def test_cookie_mutations_and_bootstrap_require_same_origin_and_loopback_host(self):
        r = await self.client.post('/session/v1/open'); self.assertEqual(r.status, 403)
        await self.open()
        for headers in [{}, {'Origin': 'https://evil.example'}, {**self.headers, 'Sec-Fetch-Site': 'cross-site'},
                        {'Host': 'attacker.example', 'Origin': 'http://attacker.example'}, {**self.headers, 'X-Forwarded-For': '192.168.1.2'}]:
            r = await self.client.post('/api/v1/leases', json={'device': 'board'}, headers=headers)
            self.assertEqual(r.status, 403)
        r = await self.client.get('/api/v1/devices', headers={'Host': 'attacker.example'})
        self.assertEqual(r.status, 403)

    async def test_cookie_agent_and_manual_operation_work_without_any_browser_token_or_grant(self):
        await self.agent()
        body = {'device': 'board', 'generation': self.generation, 'command': 'identify'}
        r = await self.client.post('/api/v1/manual-operations', json=body, headers=self.headers)
        self.assertEqual(r.status, 202); op = await r.json()
        offer = await self.ws.receive_json(); self.assertTrue(offer['local_control'])
        await self.ws.send_json({'type': 'ready', 'id': offer['id']}); self.assertEqual((await self.ws.receive_json())['type'], 'execute')
        await self.ws.send_json({'type': 'result', 'id': op['id'], 'state': 'completed'})
        self.assertEqual((await self.ws.receive_json())['type'], 'result_ack')
        r = await self.client.post('/api/v1/leases', json={'device': 'board', 'ttl': 60}, headers=BOB)
        self.assertEqual(r.status, 409)
        await self.ws.send_json({'type': 'settled', 'device': 'board', 'generation': self.generation, 'id': op['id']})
        for _ in range(50):
            r = await self.client.get('/api/v1/devices'); d = (await r.json())['devices'][0]
            if d['reservation'] is None: break
            await asyncio.sleep(.01)
        self.assertIsNone(d['reservation']); self.assertEqual(d['privileged_seconds'], 0)

    async def test_local_confirmation_does_not_enable_external_programming_and_fields_cannot_forge_local_role(self):
        await self.agent()
        body = {'device': 'board', 'generation': self.generation, 'command': 'flash.erase', 'args': {'chip': 'esp32s3'}}
        r = await self.client.post('/api/v1/manual-operations', json=body, headers=self.headers)
        self.assertEqual((await r.json())['error']['code'], 'destructive_consent_required')
        body['allow_destructive'] = True
        r = await self.client.post('/api/v1/manual-operations', json=body, headers=ALICE)
        self.assertEqual((await r.json())['error']['code'], 'grant_required')
        r = await self.client.post('/api/v1/manual-operations', json={**body, 'local_control': True}, headers=self.headers)
        self.assertEqual(r.status, 400)
        r = await self.client.post('/api/v1/manual-operations', json=body, headers=self.headers)
        self.assertEqual(r.status, 202)
        op = await r.json(); self.assertTrue(op['local_control'])
        # Queued request is cancelled; no hardware execution in this test.
        r = await self.client.delete('/api/v1/operations/' + op['id'], headers=self.headers)
        self.assertEqual((await r.json())['state'], 'cancelled')
        r = await self.client.get('/api/v1/devices'); d = (await r.json())['devices'][0]
        self.assertIsNone(d['reservation']); self.assertEqual(d['privileged_seconds'], 0)

    async def test_expired_session_loses_api_and_websocket_access(self):
        await self.agent()
        for session in self.app[SESSIONS].entries.values(): session['until'] = 0
        r = await self.client.get('/api/v1/devices'); self.assertEqual(r.status, 401)
        frame = await self.ws.receive(timeout=2)
        self.assertNotEqual(frame.type.name, 'TEXT')

    async def test_cookie_agent_readback_is_pinned_then_cleanable_by_api_operation_owner(self):
        await self.agent()
        r = await self.client.post('/api/v1/leases',json={'device':'board','ttl':60},headers=ALICE)
        lease = await r.json()
        r = await self.client.post('/api/v1/operations',headers=ALICE,json={'lease':lease['id'],
            'command':'flash.read','args':{'address':0,'size':4096}})
        self.assertEqual(r.status,202)
        op = await r.json()
        offer = await self.ws.receive_json()
        await self.ws.send_json({'type':'ready','id':offer['id']})
        self.assertEqual((await self.ws.receive_json())['type'],'execute')
        data = b'x'*4096
        headers = {**self.headers,'X-SHA256':hashlib.sha256(data).hexdigest(),
            'X-Bench-Artifact-Role':'agent'}
        r = await self.client.post('/api/v1/artifacts',data=data,headers=headers)
        self.assertEqual(r.status,201)
        artifact = await r.json()
        broker = self.app[BROKER]
        self.assertTrue(broker.artifact_pinned(artifact['id']))
        r = await self.client.delete('/api/v1/artifacts/'+artifact['id'],headers=self.headers)
        self.assertNotEqual(r.status,200)
        await self.ws.send_json({'type':'result','id':op['id'],'state':'completed','result':{'artifact':artifact}})
        self.assertEqual((await self.ws.receive_json())['type'],'result_ack')
        await self.ws.send_json({'type':'settled','device':'board','generation':self.generation,'id':op['id']})
        for _ in range(50):
            r = await self.client.get('/api/v1/operations/'+op['id'],headers=ALICE)
            if (await r.json())['settled']: break
            await asyncio.sleep(.01)
        r = await self.client.delete('/api/v1/artifacts/'+artifact['id'],headers=BOB)
        self.assertEqual(r.status,403)
        r = await self.client.delete('/api/v1/artifacts/'+artifact['id'],headers=ALICE)
        self.assertEqual(r.status,200)

    async def test_artifact_role_cannot_forge_agent_identity_or_relabel_client_uploads(self):
        await self.agent()
        data = b'x'
        digest = hashlib.sha256(data).hexdigest()
        for headers in [{**ALICE,'X-Bench-Artifact-Role':'agent'},
                        {**self.headers,'X-Bench-Artifact-Role':'arbitrary-agent'}]:
            r = await self.client.post('/api/v1/artifacts',data=data,headers={**headers,'X-SHA256':digest})
            self.assertIn(r.status,{400,403})
        r = await self.client.post('/api/v1/artifacts',data=data,headers={**self.headers,'X-SHA256':digest})
        self.assertEqual(r.status,201)
        artifact = await r.json()
        r = await self.client.delete('/api/v1/artifacts/'+artifact['id'],headers=self.headers)
        self.assertEqual(r.status,200)

class LocalBoundaryTests(unittest.TestCase):
    def test_private_network_and_rebinding_hosts_cannot_bootstrap_a_privileged_session(self):
        for remote, host in [('192.168.1.2', 'localhost:8080'), ('127.0.0.1', 'attacker.example:8080')]:
            with self.assertRaises(Fault): local_request(SimpleNamespace(remote=remote, host=host, scheme='http', headers={'Origin': 'http://' + host}), mutation=True)

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import ClientSession, CookieJar
from server.ui_sessions import UIAccess
from protocol import Fault
from test_http import TOKENS
from local_forward_fixture import LocalForward


def request(peer='172.18.0.1', origin='http://localhost:8080', **headers):
    return SimpleNamespace(remote=peer, host='bench.internal:8000', scheme='http', secure=False,
                           headers={'Origin': origin, 'X-Forwarded-For': '127.0.0.1', **headers})


class ForwardPolicyTests(unittest.TestCase):
    def test_explicit_peer_and_fixed_local_origin_allow_container_forwarding(self):
        access = UIAccess('http://localhost:8080', ['172.18.0.1'])
        access.check(request(), mutation=True)
        access.check(request(origin=None))
        self.assertFalse(access.secure_cookie(request()))

    def test_forward_headers_cannot_promote_an_untrusted_peer(self):
        access = UIAccess('http://localhost:8080', ['172.18.0.1/32'])
        for peer in ['172.18.0.2', '192.168.1.20', '203.0.113.2']:
            with self.assertRaises(Fault): access.check(request(peer=peer), mutation=True)
        with self.assertRaises(Fault): UIAccess().check(request(), mutation=True)

    def test_forwarded_origin_headers_do_not_override_configured_origin(self):
        access = UIAccess('http://localhost:8080', ['172.18.0.1'])
        for origin in [None, 'http://localhost:8081', 'http://evil.example']:
            with self.assertRaises(Fault): access.check(request(origin=origin, **{'X-Forwarded-Host': 'localhost:8080'}), mutation=True)
        with self.assertRaises(Fault): access.check(request(**{'Sec-Fetch-Site': 'cross-site'}), mutation=True)

    def test_forwarding_requires_explicit_local_origin_and_https_cookie_uses_that_origin(self):
        for origin in [None, 'http://192.168.1.2:8080', 'http://localhost:8080/path', 'http://user:pass@localhost:8080']:
            with self.assertRaises(Fault): UIAccess(origin, ['172.18.0.1'])
        with self.assertRaises(ValueError): UIAccess('http://localhost:8080', ['not-a-network'])
        access = UIAccess('https://localhost:9443', ['172.18.0.1'])
        self.assertTrue(access.secure_cookie(request(origin='https://localhost:9443')))


class ForwardedSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_and_websocket_forward_support_session_and_manual_control(self):
        forward = await LocalForward().start(TOKENS)
        try:
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
                headers = {'Origin': forward.url}
                async with client.post(forward.url + '/session/v1/open', headers=headers) as r:
                    self.assertEqual(r.status, 200); session = await r.json()
                    self.assertNotIn('token', session); self.assertIn('HttpOnly', r.headers['Set-Cookie'])
                async with client.ws_connect(forward.url + '/agent/v1/ws', headers=headers) as ws:
                    await ws.send_json({'type': 'auth', 'version': 1})
                    self.assertEqual((await ws.receive_json())['type'], 'welcome')
                    await ws.send_json({'type': 'register', 'device': {'id': 'board', 'chip': 'esp32s3', 'flash_size': 4194304, 'capabilities': ['identify']}})
                    generation = (await ws.receive_json())['generation']
                    body = {'device': 'board', 'generation': generation, 'command': 'identify'}
                    async with client.post(forward.url + '/api/v1/manual-operations', json=body, headers=headers) as r:
                        self.assertEqual(r.status, 202); op = await r.json()
                    self.assertEqual((await ws.receive_json())['type'], 'offer')
                    await ws.send_json({'type': 'ready', 'id': op['id']}); self.assertEqual((await ws.receive_json())['type'], 'execute')
                    await ws.send_json({'type': 'result', 'id': op['id'], 'state': 'completed', 'result': {'simulated': True}})
                    self.assertEqual((await ws.receive_json())['type'], 'result_ack')
                    await ws.send_json({'type': 'settled', 'device': 'board', 'generation': generation, 'id': op['id']})
                    for _ in range(50):
                        async with client.get(forward.url + '/api/v1/devices') as r: d = (await r.json())['devices'][0]
                        if d['reservation'] is None: break
                        await asyncio.sleep(.01)
                    self.assertIsNone(d['reservation'])
                    async with client.post(forward.url + '/session/v1/open', headers={'Origin': 'http://evil.example'}) as r:
                        self.assertEqual(r.status, 403)
                    async with client.get(forward.url + '/api/v1/devices', headers={'Authorization': 'Bearer invalid-invalid-invalid'}) as r:
                        self.assertEqual(r.status, 401)
                self.assertTrue(forward.http_forwarded); self.assertTrue(forward.ws_forwarded)
        finally:
            await forward.close()

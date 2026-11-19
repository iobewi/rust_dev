"""Integration of the P4 diagnostic through the REAL HTTP server and a scripted SIMULATED agent (websocket).
Software only: no board, no Web Serial. It proves the server<->agent contract and the authorization boundaries."""
import asyncio
import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestClient, TestServer
from server.bench_server import create_app
from server.core import Broker
from protocol import p4_trace_consent

TOKENS = {
    'client-alice-123456789': {'id': 'alice', 'role': 'client', 'scopes': ['read', 'control', 'program']},
    'client-bob-1234567890': {'id': 'bob', 'role': 'client', 'scopes': ['read', 'control']},
    'agent-p4-secret-123456': {'id': 'agentp4', 'role': 'agent', 'devices': ['p4']},
    'agent-c6-secret-123456': {'id': 'agentc6', 'role': 'agent', 'devices': ['c6']},
}
ALICE = {'Authorization': 'Bearer client-alice-123456789'}
BOB = {'Authorization': 'Bearer client-bob-1234567890'}
CMD = 'diagnostic.p4_usb_trace'
STUB = json.dumps({'entry': 0x4ff50010, 'text': base64.b64encode(bytes(100)).decode(), 'text_start': 0x4ff50000,
                   'data': base64.b64encode(bytes(40)).decode(), 'data_start': 0x4ff60040, 'bss_start': 0x4ff60000}).encode()
SHA = hashlib.sha256(STUB).hexdigest()


class P4HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(TOKENS, broker=Broker(p4_stub_allowlist=[SHA, hashlib.sha256(b'{"entry": 1}').hexdigest()]))))
        await self.client.start_server()
        self.sockets = []

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.client.close()

    async def agent(self, token, device, chip, transport, caps):
        ws = await self.client.ws_connect('/agent/v1/ws')
        self.sockets.append(ws)
        await ws.send_json({'type': 'auth', 'version': 1, 'token': token})
        self.assertEqual((await ws.receive_json())['type'], 'welcome')
        await ws.send_json({'type': 'register', 'device': {'id': device, 'chip': chip, 'flash_size': 32 * 1024 * 1024, 'capabilities': caps,
                            'provenance': {'transport': transport, 'transport_basis': 'inferred_from_webserial_vid_pid'}}})
        reply = await ws.receive_json()
        self.assertEqual(reply['type'], 'registered')
        self.registration = reply
        return ws, reply['generation']

    async def p4(self):
        self.p4ws, self.gen = await self.agent('agent-p4-secret-123456', 'p4', 'esp32p4', 'native_usb_other',
                                               ['identify', 'flash.read', 'partitions', 'mark', 'flash.write', CMD])
        await self.p4ws.send_json({'type': 'grant', 'device': 'p4', 'generation': self.gen, 'seconds': 1800})
        r = await self.client.post('/api/v1/leases', json={'device': 'p4', 'ttl': 300}, headers=ALICE)
        self.assertEqual(r.status, 201)
        self.lease = (await r.json())['id']

    async def upload(self, data=STUB, sha=None):
        r = await self.client.post('/api/v1/artifacts', data=data, headers={**ALICE, 'X-SHA256': sha or hashlib.sha256(data).hexdigest()})
        self.assertEqual(r.status, 201 if sha is None else r.status)
        return (await r.json())

    def args(self, artifact, **over):
        a = {'chip': 'esp32p4', 'stub_artifact': artifact['id'], 'stub_sha256': artifact['sha256'], 'address': 0x8000, 'size': 1048576,
             'packet_size': 4096, 'cpu_hz': 200000000, 'consent': p4_trace_consent('p4', artifact['sha256'])}
        a.update(over)
        return a

    async def submit(self, args, headers=ALICE, consent=True, command=CMD, lease=None):
        return await self.client.post('/api/v1/operations', headers=headers,
                                      json={'lease': lease or self.lease, 'command': command, 'args': args, 'allow_destructive': consent})

    async def code(self, response):
        return (await response.json()).get('error', {}).get('code')

    async def run_to_result(self, ws, op, state='completed', result=None, hold=False):
        offer = await ws.receive_json()
        self.assertEqual((offer['id'], offer.get('command')), (op['id'], CMD))
        await ws.send_json({'type': 'ready', 'id': op['id']})
        self.assertEqual((await ws.receive_json())['type'], 'execute')
        await ws.send_json({'type': 'result', 'id': op['id'], 'state': state, 'result': result or {'md5': 'ab' * 16}, 'hold': hold})
        self.assertEqual((await ws.receive_json())['type'], 'result_ack')

    async def wait_for(self, predicate, what):
        for _ in range(100):
            if await predicate():
                return
            await asyncio.sleep(.02)
        self.fail(what)

    async def device(self, name='p4'):
        r = await self.client.get('/api/v1/devices', headers=ALICE)
        return next(d for d in (await r.json())['devices'] if d['id'] == name)

    # ---------------------------------------------------------------------------------- nominal
    async def test_nominal_run_quarantines_the_board_and_only_local_recovery_frees_it(self):
        await self.p4()
        art = await self.upload()
        r = await self.submit(self.args(art))
        self.assertEqual(r.status, 202, await r.text())
        op = await r.json()
        await self.run_to_result(self.p4ws, op)
        await self.p4ws.send_json({'type': 'settled', 'id': op['id'], 'device': 'p4', 'generation': self.gen})
        done = await (await self.client.get('/api/v1/operations/' + op['id'], headers=ALICE)).json()
        self.assertEqual(done['state'], 'completed')
        dev = await self.device()
        self.assertTrue(dev['quarantined'] and dev['diagnostic_resident'])
        for command, args in (('flash.read', {'address': 0x8000, 'size': 4096}), ('identify', {}), ('partitions', {})):
            r = await self.submit(args, command=command, consent=False)
            self.assertEqual((r.status, await self.code(r)), (409, 'device_quarantined'), command)
        await self.p4ws.send_json({'type': 'recover', 'device': 'p4', 'generation': self.gen})
        self.assertEqual((await self.p4ws.receive_json())['error']['code'], 'rom_proof_required')
        self.assertTrue((await self.device())['diagnostic_resident'])
        await self.p4ws.send_json({'type': 'recover', 'device': 'p4', 'generation': self.gen, 'rom_verified': True})
        self.assertEqual((await self.p4ws.receive_json())['type'], 'recovered')
        dev = await self.device()
        self.assertFalse(dev['quarantined'] or dev['diagnostic_resident'])

    # ---------------------------------------------------------------------------------- refusals (nothing is ever offered)
    async def test_consent_refusal_and_bad_sha_and_scope_create_no_operation(self):
        await self.p4()
        art = await self.upload()
        cases = [(self.args(art, consent={}), ALICE, True, 403, 'diagnostic_consent_invalid'),
                 (self.args(art, consent=p4_trace_consent('p4', '0' * 64)), ALICE, True, 403, 'diagnostic_consent_invalid'),
                 (self.args(art, stub_sha256='0' * 64, consent=p4_trace_consent('p4', '0' * 64)), ALICE, True, 400, 'digest_mismatch'),
                 (self.args(art), ALICE, False, 403, 'destructive_consent_required'),
                 (self.args(art), BOB, True, 403, 'forbidden'),   # not the lease owner (scope check itself: test_p4_usb_trace_core)
                 (self.args(art, size=4096), ALICE, True, 400, 'diagnostic_parameter_not_allowed')]
        for args, headers, consent, status, code in cases:
            r = await self.submit(args, headers=headers, consent=consent)
            self.assertEqual((r.status, await self.code(r)), (status, code), code)
        r = await self.client.get('/api/v1/operations', headers=ALICE)
        ops = (await r.json()).get('operations', []) if r.status == 200 else []
        self.assertEqual([o for o in ops if o['command'] == CMD], [])
        await self.assertNothingOffered()

    async def assertNothingOffered(self):
        """No operation offer reaches the agent (lease_revoked and similar notices are not offers)."""
        loop = asyncio.get_running_loop(); end = loop.time() + .3
        while (left := end - loop.time()) > 0:
            try:
                message = await asyncio.wait_for(self.p4ws.receive_json(), timeout=left)
            except asyncio.TimeoutError:
                return
            self.assertNotIn('command', message, f'an operation was offered to the agent: {message}')

    async def test_a_stub_that_does_not_conform_is_refused_before_the_agent_is_involved(self):
        await self.p4()
        bad = b'{"entry": 1}'
        art = await self.upload(bad)
        r = await self.submit(self.args(art))
        self.assertEqual(await self.code(r), 'diagnostic_stub_invalid')
        await self.assertNothingOffered()

    async def test_an_agent_that_does_not_advertise_the_command_is_refused(self):
        ws, gen = await self.agent('agent-p4-secret-123456', 'p4', 'esp32p4', 'native_usb_other', ['identify', 'flash.read'])
        await ws.send_json({'type': 'grant', 'device': 'p4', 'generation': gen, 'seconds': 1800})
        self.p4ws = ws
        r = await self.client.post('/api/v1/leases', json={'device': 'p4', 'ttl': 60}, headers=ALICE)
        art = await self.upload()
        r = await self.client.post('/api/v1/operations', headers=ALICE, json={'lease': (await r.json())['id'], 'command': CMD, 'args': self.args(art), 'allow_destructive': True})
        self.assertGreaterEqual(r.status, 400)
        await self.assertNothingOffered()

    # ---------------------------------------------------------------------------------- concurrency and isolation
    async def test_concurrent_attempt_on_the_same_board_is_refused_and_the_other_board_is_independent(self):
        await self.p4()
        c6, c6gen = await self.agent('agent-c6-secret-123456', 'c6', 'esp32c6', 'usb_serial_jtag', ['identify', 'flash.read', 'mark'])
        art = await self.upload()
        r = await self.submit(self.args(art)); op = await r.json()
        self.assertEqual(r.status, 202)
        r = await self.submit(self.args(art))
        self.assertEqual((r.status, await self.code(r)), (409, 'diagnostic_in_progress'))
        r = await self.submit({'address': 0x8000, 'size': 4096}, command='flash.read', consent=False)
        self.assertEqual((r.status, await self.code(r)), (409, 'diagnostic_in_progress'))
        # the other board runs a standard read normally while the P4 diagnostic is pending
        lease = (await (await self.client.post('/api/v1/leases', json={'device': 'c6', 'ttl': 60}, headers=ALICE)).json())['id']
        r = await self.client.post('/api/v1/operations', headers=ALICE, json={'lease': lease, 'command': 'flash.read', 'args': {'address': 0, 'size': 4096}})
        self.assertEqual(r.status, 202, await r.text())
        offer = await c6.receive_json()
        self.assertEqual(offer['command'], 'flash.read')
        await self.run_to_result(self.p4ws, op)

    # ---------------------------------------------------------------------------------- interruptions
    async def test_agent_loss_during_the_run_is_indeterminate_and_survives_reconnection(self):
        await self.p4()
        art = await self.upload()
        op = await (await self.submit(self.args(art))).json()
        offer = await self.p4ws.receive_json(); self.assertEqual(offer['id'], op['id'])
        await self.p4ws.send_json({'type': 'ready', 'id': op['id']})
        self.assertEqual((await self.p4ws.receive_json())['type'], 'execute')
        await self.p4ws.close()
        async def indeterminate():
            return (await (await self.client.get('/api/v1/operations/' + op['id'], headers=ALICE)).json())['state'] == 'indeterminate'
        await self.wait_for(indeterminate, 'operation did not become indeterminate')
        self.p4ws, self.gen = await self.agent('agent-p4-secret-123456', 'p4', 'esp32p4', 'native_usb_other',
                                               ['identify', 'flash.read', 'partitions', 'mark', 'flash.write', CMD])
        dev = await self.device()
        self.assertTrue(dev['quarantined'] and dev['diagnostic_resident'])
        self.assertTrue(self.registration['diagnostic_resident'] and self.registration['quarantined'])   # what a reloaded agent is told
        r = await self.client.post('/api/v1/leases', json={'device': 'p4', 'ttl': 60}, headers=ALICE)
        lease = (await r.json()).get('id', self.lease)
        r = await self.client.post('/api/v1/operations', headers=ALICE, json={'lease': lease, 'command': 'flash.read', 'args': {'address': 0x8000, 'size': 4096}})
        self.assertEqual((r.status, await self.code(r)), (409, 'device_quarantined'))
        await self.assertNothingOffered()

    async def test_an_indeterminate_result_from_the_agent_is_kept_as_such(self):
        await self.p4()
        art = await self.upload()
        op = await (await self.submit(self.args(art))).json()
        await self.run_to_result(self.p4ws, op, state='indeterminate', result=None, hold=True)
        got = await (await self.client.get('/api/v1/operations/' + op['id'], headers=ALICE)).json()
        self.assertEqual(got['state'], 'indeterminate')
        self.assertTrue((await self.device())['quarantined'])


if __name__ == '__main__':
    unittest.main()

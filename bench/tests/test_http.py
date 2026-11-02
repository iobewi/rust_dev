import asyncio
import base64
import hashlib
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestClient, TestServer
from server.bench_server import create_app
from examples.client import BenchClient
from tools.sim_agent import run

TOKENS={
 'client-alice-123456789':{'id':'alice','role':'client','scopes':['read','control','program']},
 'client-bob-1234567890':{'id':'bob','role':'client','scopes':['read','control']},
 'readonly-12345678901':{'id':'reader','role':'client','scopes':['read']},
 'agent-secret-12345678':{'id':'agent','role':'agent','devices':['board']},
}
ALICE={'Authorization':'Bearer client-alice-123456789'}
BOB={'Authorization':'Bearer client-bob-1234567890'}
AGENT={'Authorization':'Bearer agent-secret-12345678'}


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=TestClient(TestServer(create_app(TOKENS)))
        await self.client.start_server()
        self.ws=None;self.sim=None

    async def asyncTearDown(self):
        if self.ws:await self.ws.close()
        if self.sim:
            self.sim.cancel()
            try:await self.sim
            except asyncio.CancelledError:pass
        await self.client.close()

    async def agent(self):
        self.ws=await self.client.ws_connect('/agent/v1/ws')
        await self.ws.send_json({'type':'auth','version':1,'token':'agent-secret-12345678'})
        self.assertEqual((await self.ws.receive_json())['type'],'welcome')
        await self.ws.send_json({'type':'register','device':{'id':'board','chip':'esp32s3','flash_size':4194304,'capabilities':['identify','flash.write','flash.read','reset']}})
        reply=await self.ws.receive_json();self.generation=reply['generation'];return self.ws

    async def reserve(self,headers=ALICE):
        r=await self.client.post('/api/v1/leases',json={'device':'board','ttl':60},headers=headers)
        return r,await r.json()

    async def test_authentication_origin_and_static_boundaries(self):
        for path in ['/api/v1/devices','/api/v1/artifacts/nope']:
            r=await self.client.get(path);self.assertEqual(r.status,401)
        r=await self.client.get('/api/v1/devices',headers={**ALICE,'Origin':'https://evil.example'})
        self.assertEqual(r.status,403)
        for path in ['/bench_server.py','/requirements.txt','/manifest.json']:
            r=await self.client.get(path);self.assertEqual(r.status,404)
        r=await self.client.get('/');self.assertEqual(r.status,200);self.assertIn('Content-Security-Policy',r.headers)
        r=await self.client.post('/api/v1/leases',json={},headers={'Authorization':'Bearer readonly-12345678901'})
        self.assertEqual(r.status,403)
        r=await self.client.get('/api/v1/devices',headers=AGENT);self.assertEqual(r.status,403)

    async def test_websocket_auth_and_device_allowlist(self):
        self.ws=await self.client.ws_connect('/agent/v1/ws')
        await self.ws.send_json({'type':'auth','version':1,'token':'invalid-invalid-invalid'})
        self.assertNotEqual((await self.ws.receive()).type.name,'TEXT')
        await self.ws.close();await self.agent()
        await self.ws.send_json({'type':'register','device':{'id':'other'}})
        self.assertEqual((await self.ws.receive_json())['error']['code'],'device_forbidden')

    async def test_concurrent_reservations(self):
        await self.agent()
        results=await asyncio.gather(self.reserve(ALICE),self.reserve(BOB))
        self.assertEqual(sorted(r.status for r,_ in results),[201,409])
        winner=next(data for r,data in results if r.status==201)
        wrong=BOB if winner['owner']=='alice' else ALICE
        r=await self.client.delete('/api/v1/leases/'+winner['id'],headers=wrong);self.assertEqual(r.status,403)

    async def test_command_lifecycle_and_binary_artifacts(self):
        await self.agent();_,lease=await self.reserve()
        r=await self.client.post('/api/v1/operations',headers=ALICE,json={'lease':lease['id'],'command':'identify'})
        self.assertEqual(r.status,202);op=await r.json()
        offer=await self.ws.receive_json();self.assertEqual(offer['type'],'offer')
        await self.ws.send_json({'type':'ready','id':offer['id']})
        self.assertEqual((await self.ws.receive_json())['type'],'execute')
        await self.ws.send_json({'type':'result','id':op['id'],'state':'completed','result':{'chip':'esp32s3'}})
        for _ in range(30):
            r=await self.client.get('/api/v1/operations/'+op['id'],headers=ALICE);done=await r.json()
            if done['state']=='completed':break
            await asyncio.sleep(.01)
        self.assertEqual(done['result']['chip'],'esp32s3')
        r=await self.client.get('/api/v1/operations/'+op['id'],headers=BOB);self.assertEqual(r.status,403)
        data=bytes(range(256))*32;digest=hashlib.sha256(data).hexdigest()
        r=await self.client.post('/api/v1/artifacts',headers={**ALICE,'X-SHA256':digest},data=data)
        self.assertEqual(r.status,201);a=await r.json()
        r=await self.client.get('/api/v1/artifacts/'+a['id'],headers=ALICE)
        self.assertEqual(await r.read(),data);self.assertEqual(r.headers['X-SHA256'],digest)
        r=await self.client.post('/api/v1/artifacts',headers={**ALICE,'X-SHA256':'bad'},data=data);self.assertEqual(r.status,400)

    async def test_agent_disconnect_never_reports_success(self):
        await self.agent();_,lease=await self.reserve()
        r=await self.client.post('/api/v1/operations',headers=ALICE,json={'lease':lease['id'],'command':'identify'});op=await r.json()
        offer=await self.ws.receive_json();await self.ws.send_json({'type':'ready','id':offer['id']});await self.ws.receive_json()
        await self.ws.close()
        for _ in range(30):
            r=await self.client.get('/api/v1/operations/'+op['id'],headers=ALICE);data=await r.json()
            if data['state']=='indeterminate':break
            await asyncio.sleep(.01)
        self.assertEqual(data['state'],'indeterminate')

    async def test_uart_and_explicit_source_loss(self):
        await self.agent()
        for seq in [1,3]:await self.ws.send_json({'type':'uart','device':'board','generation':self.generation,'seq':seq,'data':base64.b64encode(b'\x00\xff').decode()})
        for _ in range(30):
            r=await self.client.get('/api/v1/devices/board/events',headers=ALICE);data=await r.json()
            if len(data['events'])>=3:break
            await asyncio.sleep(.01)
        self.assertEqual(data['events'][-1]['source_lost'],1)

    async def test_complete_simulation_with_independent_api_client(self):
        url=str(self.client.make_url('')).rstrip('/')
        self.sim=asyncio.create_task(run(url,'agent-secret-12345678','board',grant=60))
        for _ in range(100):
            r=await self.client.get('/api/v1/devices',headers=ALICE)
            if (await r.json())['devices']:break
            await asyncio.sleep(.01)
        from aiohttp import ClientSession
        async with ClientSession(headers=ALICE) as session:
            api=BenchClient(session,url)
            lease=await api.request('POST','leases',json={'device':'board','ttl':60})
            ident=await api.command(lease['id'],'identify');self.assertTrue(ident['simulated'])
            data=bytes(range(256))*100;artifact=await api.upload(data)
            result=await api.command(lease['id'],'flash.write',{'chip':'esp32s3','return_console':False,
                'images':[{'artifact':artifact['id'],'sha256':artifact['sha256'],'address':0x10000}]},True)
            self.assertEqual(result['written'],1)
            read=await api.command(lease['id'],'flash.read',{'address':0x10000,'size':len(data),'return_console':False})
            self.assertEqual(await api.download(read['artifact']),data)
            await api.command(lease['id'],'reset')
            events=await api.request('GET','devices/board/events')
            self.assertTrue(any(e['kind']=='uart' for e in events['events']))
            await api.request('DELETE','leases/'+lease['id'])

    async def test_json_size_limit_and_invalid_schema(self):
        r=await self.client.post('/api/v1/leases',headers=ALICE,data=b'{'+b' '*65536+b'}')
        self.assertEqual(r.status,413)
        r=await self.client.post('/api/v1/leases',headers=ALICE,json=[]);self.assertEqual(r.status,400)

    async def test_sse_subscription_and_artifact_inventory(self):
        await self.agent()
        r=await self.client.get('/api/v1/devices/board/events/stream',headers=ALICE)
        self.assertEqual(r.status,200);self.assertEqual(r.headers['Content-Type'],'text/event-stream')
        line=await r.content.readline();self.assertTrue(line.startswith(b'data: '))
        import json
        self.assertEqual(json.loads(line[6:])['events'][0]['kind'],'connected');r.close()
        r=await self.client.get('/api/v1/artifacts',headers=ALICE);self.assertEqual((await r.json())['artifacts'],[])

#!/usr/bin/env python3
"""Deterministic API agent. Simulation only, never evidence about a real ESP32."""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import struct
import time
from aiohttp import ClientSession

CAPS = ['identify', 'reset', 'partitions', 'flash.read', 'flash.write', 'flash.erase',
        'uart.write', 'uart.configure', 'console', 'io0', 'boot.download', 'mark', 'trigger']


class SimFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SimDevice:
    def __init__(self, capacity=4 * 1024 * 1024, chip='esp32s3'):
        self.chip = chip
        self.flash = bytearray(b'\xff') * capacity
        self.writes = 0

    async def execute(self, name, args, download, upload):
        if name == 'identify':
            return {'chip': self.chip, 'flash_size': len(self.flash), 'simulated': True}
        if name == 'flash.write':
            images = [(i, await download(i['artifact'])) for i in args['images']]
            for i, data in images:
                if hashlib.sha256(data).hexdigest() != i['sha256']:
                    raise ValueError('digest mismatch')
                start = i['address']
                if start < 0 or start + len(data) > len(self.flash):
                    raise ValueError('bounds')
            if args.get('erase'):
                self.flash[:] = b'\xff' * len(self.flash)
            for i, data in images:
                start = i['address']
                lo, hi = start // 4096 * 4096, (start + len(data) + 4095) // 4096 * 4096
                self.flash[lo:hi] = b'\xff' * (hi-lo)
                self.flash[start:start+len(data)] = data
            self.writes += 1
            return {'written': len(images), 'simulated': True}
        if name == 'flash.read':
            data = bytes(self.flash[args['address']:args['address']+args['size']])
            return {'artifact': await upload(data), 'simulated': True}
        if name == 'flash.erase':
            self.flash[:] = b'\xff' * len(self.flash)
            return {'erased': True, 'simulated': True}
        if name == 'partitions':
            raw, rows = bytes(self.flash[args['address']:args['address'] + 0xC00]), []
            for offset in range(0, 0xC00, 32):
                entry = raw[offset:offset + 32]
                if entry[:2] != b'\xaa\x50':
                    break
                kind, subtype, start, size = struct.unpack('<BBII', entry[2:12])
                rows.append({'label': entry[12:28].split(b'\0', 1)[0].decode(), 'type': kind, 'subtype': subtype,
                             'address': start, 'size': size, 'flags': struct.unpack('<I', entry[28:32])[0]})
            if not rows:
                raise SimFailure('partition_table_invalid')
            return {'partitions': rows, 'simulated': True}
        if name == 'boot.download':
            return {'download_mode_requested': True, 'method': 'en+io0', 'hold_ms': args['hold_ms'], 'boot_verified': False, 'simulated': True}
        return {'command': name, 'simulated': True}


async def run(url, token, device_id, grant=0, stop=None, script=(), chip='esp32s3'):
    """`script`: simulated operator gestures [(delay_s, grant_seconds)], 0 revokes. Simulation only."""
    sim = SimDevice(chip=chip)
    async with ClientSession(headers={'Authorization': 'Bearer ' + token}) as session:
        async with session.ws_connect(url + '/agent/v1/ws') as ws:
            await ws.send_json({'type': 'auth', 'version': 1, 'token': token})
            await ws.receive_json()
            await ws.send_json({'type': 'register', 'device': {'id': device_id, 'chip': chip,
                                'flash_size': len(sim.flash), 'capabilities': CAPS}})
            registered = await ws.receive_json()
            generation, seq = registered['generation'], 0
            async def event(kind, **payload):
                await ws.send_json({'type': kind, 'device': device_id, 'generation': generation, **payload})
            if grant:
                await event('grant', seconds=grant)
            async def operator():
                start = time.monotonic()
                for delay, seconds in script:
                    await asyncio.sleep(max(0, start + delay - time.monotonic()))
                    await event('grant', seconds=seconds)
            gestures = asyncio.create_task(operator())
            pending = {}
            async def download(key):
                async with session.get(url + '/api/v1/artifacts/' + key) as r:
                    r.raise_for_status(); return await r.read()
            async def upload(data):
                async with session.post(url + '/api/v1/artifacts', data=data,
                    headers={'X-SHA256': hashlib.sha256(data).hexdigest()}) as r:
                    r.raise_for_status(); return await r.json()
            print(json.dumps({'simulation': True, 'device': device_id, 'connected': True}), flush=True)
            async for frame in ws:
                if frame.type.name != 'TEXT':
                    break
                m = json.loads(frame.data)
                if m['type'] == 'offer':
                    pending[m['id']] = m
                    await ws.send_json({'type': 'ready', 'id': m['id']})
                elif m['type'] == 'result_ack':
                    await event('settled', id=m['id'])
                elif m['type'] == 'execute':
                    op = pending.pop(m['id'], None)
                    if not op:
                        continue
                    try:
                        result = await sim.execute(op['command'], op['args'], download, upload)
                        if op['command'] in {'reset', 'uart.write', 'boot.download'}:
                            seq += 1
                            data = (('SIM boot\r\nESP-ROM:%s-sim\r\n' % chip).encode() if op['command'] == 'reset' else
                                    ('SIM download mode\r\nESP-ROM:%s-sim\r\nSIM-DOWNLOAD-MODE\r\n' % chip).encode() if op['command'] == 'boot.download'
                                    else base64.b64decode(op['args']['data']))
                            await event('uart', seq=seq, data=base64.b64encode(data).decode(), at=time.time()*1000)
                        await ws.send_json({'type': 'result', 'id': op['id'], 'state': 'completed', 'result': result})
                    except Exception as error:
                        await ws.send_json({'type': 'result', 'id': op['id'], 'state': 'failed', 'error': {'code': getattr(error, 'code', 'simulated_failure')}})
            gestures.cancel()
    return sim


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8080')
    parser.add_argument('--device', default='esp32-local')
    parser.add_argument('--grant', type=int, default=0, help='Explicit simulated local grant in seconds')
    a = parser.parse_args()
    asyncio.run(run(a.url.rstrip('/'), os.environ['BENCH_AGENT_TOKEN'], a.device, a.grant))

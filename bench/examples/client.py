#!/usr/bin/env python3
"""Generic API client and binary round-trip example. Reports belong to the caller."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from aiohttp import ClientSession


class BenchClient:
    def __init__(self, session, url):
        self.session, self.url = session, url.rstrip('/') + '/api/v1/'

    async def request(self, method, path, **kwargs):
        async with self.session.request(method, self.url + path, **kwargs) as r:
            data = await r.json()
            if r.status >= 400:
                raise RuntimeError(data['error']['code'])
            return data

    async def command(self, lease, name, args=None, allow_destructive=False):
        op = await self.request('POST', 'operations', json={'lease': lease, 'command': name,
                   'args': args or {}, 'allow_destructive': allow_destructive})
        deadline = asyncio.get_running_loop().time() + 650
        while op['state'] not in {'completed', 'failed', 'expired', 'indeterminate', 'cancelled'}:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError('Client wait ended; this does not cancel hardware execution')
            await asyncio.sleep(.05)
            op = await self.request('GET', 'operations/' + op['id'])
        if op['state'] != 'completed':
            raise RuntimeError(json.dumps(op))
        return op['result']

    async def events(self, device, after=0):
        """Yield bounded UART/event batches; the caller handles epoch changes and loss."""
        async with self.session.get(self.url + f'devices/{device}/events/stream?after={after}') as r:
            r.raise_for_status()
            async for line in r.content:
                if line.startswith(b'data: '):
                    yield json.loads(line[6:])

    async def upload(self, data):
        return await self.request('POST', 'artifacts', data=data, headers={'X-SHA256': hashlib.sha256(data).hexdigest()})

    async def download(self, artifact):
        async with self.session.get(self.url + 'artifacts/' + artifact['id']) as r:
            r.raise_for_status(); data = await r.read()
        if hashlib.sha256(data).hexdigest() != artifact['sha256'] or len(data) != artifact['size']:
            raise ValueError('Corrupt artifact')
        return data


async def main(args):
    async with ClientSession(headers={'Authorization': 'Bearer ' + os.environ['BENCH_CLIENT_TOKEN']}) as s:
        client = BenchClient(s, args.url)
        lease = await client.request('POST', 'leases', json={'device': args.device, 'ttl': 900})
        try:
            print(json.dumps(await client.command(lease['id'], 'identify'), indent=2))
            if args.image:
                if not args.allow_destructive:
                    raise ValueError('--image requires --allow-destructive (plus a local agent grant)')
                data = Path(args.image).read_bytes(); artifact = await client.upload(data)
                await client.command(lease['id'], 'flash.write', {'chip': args.chip, 'return_console': False,
                    'images': [{'artifact': artifact['id'], 'sha256': artifact['sha256'], 'address': args.address}]}, True)
                result = await client.command(lease['id'], 'flash.read', {'address': args.address, 'size': len(data), 'return_console': False})
                if await client.download(result['artifact']) != data:
                    raise ValueError('Read-back differs')
                print('Binary round-trip verified. This is hardware evidence only when using a real hardware agent.')
        finally:
            await client.request('DELETE', 'leases/' + lease['id'])


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='http://127.0.0.1:8080'); p.add_argument('--device', default='esp32-local')
    p.add_argument('--image'); p.add_argument('--address', type=lambda x: int(x, 0), default=0x10000)
    p.add_argument('--chip', default='esp32s3'); p.add_argument('--allow-destructive', action='store_true')
    asyncio.run(main(p.parse_args()))

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
from aiohttp import ClientSession, ClientTimeout

CAPS = ['identify', 'reset', 'partitions', 'flash.read', 'flash.write', 'flash.erase',
        'uart.write', 'uart.configure', 'console', 'io0', 'boot.download', 'mark', 'trigger', 'http.request']
NATIVE_CAPS = [c for c in CAPS if c not in {'io0', 'boot.download'}]     # a native USB Serial/JTAG card offers neither (SIM_NATIVE=1)


class SimFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SimDevice:
    def __init__(self, capacity=4 * 1024 * 1024, chip='esp32s3'):
        self.chip = chip
        self.flash = bytearray(b'\xff') * capacity
        for item in json.loads(os.environ.get('SIM_FLASH_PRELOAD', '[]')):          # SIMULATION ONLY: known flash content ([{address, hex}, ...])
            raw = bytes.fromhex(item['hex'])
            self.flash[item['address']:item['address'] + len(raw)] = raw
        self.writes = 0
        self.trigger, self.trigger_id, self.dropped = None, 0, False

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
            if args.get('erase') or os.environ.get('SIM_FAULT_GLOBAL_ERASE') == '1':      # the env var SIMULATES an implicit global erase (test oracle)
                self.flash[:] = b'\xff' * len(self.flash)
            for i, data in images:
                start = i['address']
                lo, hi = start // 4096 * 4096, (start + len(data) + 4095) // 4096 * 4096
                self.flash[lo:hi] = b'\xff' * (hi-lo)
                self.flash[start:start+len(data)] = data
                if os.environ.get('SIM_FAULT_CORRUPT_WRITE') == '1' and len(images) > 1:   # SIMULATES a multi-image write that does not read back as written
                    self.flash[start] ^= 0xFF
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
        if name == 'http.request':
            # SIMULATES the page agent contract: no credentials, no referrer, redirects refused, opaque result. Real network access to the SIM_HTTP_ORIGINS target.
            async with ClientSession() as http:
                async with http.get(args['url'], allow_redirects=False, headers={'Cache-Control': 'no-store'}, timeout=ClientTimeout(total=5)) as response:
                    if 300 <= response.status < 400:
                        raise SimFailure('redirect_refused')
            return {'response': 'opaque', 'http_status': 'unknown', 'verified': False, 'simulated': True}
        if name == 'boot.download':
            return {'download_mode_requested': True, 'method': 'en+io0', 'hold_ms': args['hold_ms'], 'boot_verified': False, 'simulated': True}
        return {'command': name, 'simulated': True}


def args_return_console(args):
    return args.get('return_console', True) is True


async def run(url, token, device_id, grant=0, stop=None, script=(), chip='esp32s3', script_start=None):
    """`script`: simulated operator gestures [(delay_s, grant_seconds)], 0 revokes. Simulation only.

    `script_start` (an asyncio.Event, optional): the script clock starts when it is set - an explicit state signal - instead of when the
    agent connects, so a gesture never races the start of the recipe under test.

    SIM_NATIVE=1 registers a native USB Serial/JTAG card (no io0 / boot.download); SIM_REENUMERATE=1 makes a `reset` re-enumerate the
    USB device (the agent disconnects and registers again with a new generation)."""
    sim = SimDevice(chip=chip)
    async with ClientSession(headers={'Authorization': 'Bearer ' + token}) as session:
        first, connection = True, 0
        while True:
            again = await serve(session, url, token, device_id, grant if first else 0, script, chip, sim, connection, script_start)
            first, connection = False, connection + 1
            if not again:
                break
            await asyncio.sleep(0.5)
    return sim


async def serve(session, url, token, device_id, grant, script, chip, sim, connection=0, script_start=None):
    """One agent connection. Returns True when the card re-enumerated (the caller reconnects)."""
    reenumerate = False
    async with session.ws_connect(url + '/agent/v1/ws') as ws:
        await ws.send_json({'type': 'auth', 'version': 1, 'token': token})
        await ws.receive_json()
        native = os.environ.get('SIM_NATIVE') == '1'
        await ws.send_json({'type': 'register', 'device': {'id': device_id, 'chip': chip, 'flash_size': len(sim.flash),
                            'capabilities': NATIVE_CAPS if native and os.environ.get('SIM_NATIVE_WITH_IO0') != '1' else CAPS,
                            'http_origins': [o for o in os.environ.get('SIM_HTTP_ORIGINS', '').split(',') if o],
                            **({'provenance': {'usb_vid': 0x303a, 'usb_pid': 0x1001, 'transport': 'usb_serial_jtag',
                                               'transport_basis': 'inferred_from_webserial_vid_pid'}} if native else {})}})
        registered = await ws.receive_json()
        generation, seq = registered['generation'], 0
        tasks = []
        uart_tag = (device_id + '|\r\n').encode() if os.environ.get('SIM_UART_TAG') == '1' else b''   # per-card source identity (SIMULATION ONLY)

        async def event(kind, **payload):
            await ws.send_json({'type': kind, 'device': device_id, 'generation': generation, **payload})

        async def emit_uart(data):
            nonlocal seq
            seq += 1
            await event('uart', seq=seq, data=base64.b64encode(data).decode(), at=time.time() * 1000)
            trigger = sim.trigger
            if trigger and trigger['pattern'] in data and time.monotonic() < trigger['until']:
                sim.trigger = None
                await event('trigger', detail='one-shot byte pattern matched')
                token_id = trigger['id']

                async def fire():
                    await asyncio.sleep(trigger['delay'] / 1000)
                    if sim.trigger_id != token_id:
                        return                                    # re-armed or disarmed meanwhile: the pending action is cancelled
                    if trigger['action'] == 'reset':
                        await emit_uart(('SIM boot\r\nESP-ROM:%s-sim\r\n' % chip).encode())
                    else:
                        await event('mark', detail='trigger')
                tasks.append(asyncio.create_task(fire()))
        if grant:
            await event('grant', seconds=grant)
        if os.environ.get('SIM_UART_RATE'):                       # SIMULATES a firmware printing continuously: "chunks_per_s:bytes[:skip_every]"
            rate, size, *skip = (int(x) for x in os.environ['SIM_UART_RATE'].split(':'))

            async def source():
                nonlocal seq
                count = 0
                while True:
                    await asyncio.sleep(1 / rate)
                    count += 1
                    if skip and count % skip[0] == 0:
                        seq += 1                                  # a chunk lost on the agent side: the next seq jumps, the server flags it
                    await emit_uart(uart_tag + b'x' * size)
            tasks.append(asyncio.create_task(source()))

        async def operator():
            if script_start is not None and connection == 0:
                await script_start.wait()                          # explicit signal, not a fixed delay from the agent's connection
            start = time.monotonic()
            for delay, seconds in script:
                if (seconds == 'recover') != (connection > 0):
                    continue                                          # numeric grants: first connection; 'recover' gestures: after a reconnection
                await asyncio.sleep(max(0, start + delay - time.monotonic()))
                await (event('recover') if seconds == 'recover' else event('grant', seconds=seconds))
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
        serial_lost = False
        async for frame in ws:
            if frame.type.name != 'TEXT':
                break
            m = json.loads(frame.data)
            if m['type'] == 'offer':
                pending[m['id']] = m
                await ws.send_json({'type': 'ready', 'id': m['id']})
            elif m['type'] == 'result_ack':
                await event('settled', id=m['id'])
                if serial_lost:
                    await asyncio.sleep(4)
                    reenumerate = True
                if reenumerate:
                    break                                         # the USB device went away: the socket closes, the caller reconnects
            elif m['type'] == 'execute':
                op = pending.pop(m['id'], None)
                if not op:
                    continue
                try:
                    command, args = op['command'], op['args']
                    if command == 'trigger':
                        sim.trigger_id += 1
                        sim.trigger = None if args.get('action') == 'disarm' else {
                            'pattern': base64.b64decode(args['data']), 'action': args['action'], 'delay': args.get('delay_ms', 0),
                            'until': time.monotonic() + args['expires_ms'] / 1000, 'id': sim.trigger_id}
                        result = {'armed': sim.trigger is not None, 'simulated': True}
                    elif command == 'mark':
                        await event('mark', detail=args['label'])             # as the page agent does: an event, no hardware effect
                        result = {'label': args['label']}
                    else:
                        result = await sim.execute(command, args, download, upload)
                    if command == 'flash.read' and os.environ.get('SIM_SERIAL_FAIL_DURING_READ') == '1':
                        await asyncio.sleep(2)                    # the serial port vanishes under the read; the browser agent stays connected
                        err = RuntimeError('serial_disconnected'); err.code = 'serial_disconnected'
                        raise err
                    if command == 'flash.read' and os.environ.get('SIM_DROP_DURING_READ') == '1' and not sim.dropped:
                        sim.dropped = True                        # the page/agent vanishes mid-operation: no result is ever reported
                        await asyncio.sleep(1)
                        reenumerate = True
                        break
                    if command == 'reset' and os.environ.get('SIM_REENUMERATE') == '1':
                        reenumerate = True                        # no banner here: it would appear after the USB device returned
                    elif command == 'reset':
                        await emit_uart(('SIM boot\r\nESP-ROM:%s-sim\r\n' % chip).encode() + uart_tag)
                    elif command == 'console' and os.environ.get('SIM_CONSOLE_BOOT'):
                        await event('transport', detail='console opened; opening a port may reset the MCU')
                        for line in json.loads(os.environ['SIM_CONSOLE_BOOT']):   # SIMULATES the ROM banner, 2nd stage and application lines
                            await emit_uart((line + '\r\n').encode() + uart_tag)
                            await asyncio.sleep(0.05)
                    elif command == 'boot.download':
                        await emit_uart(('SIM download mode\r\nESP-ROM:%s-sim\r\nSIM-DOWNLOAD-MODE\r\n' % chip).encode())
                    elif command == 'uart.write':
                        await emit_uart(base64.b64decode(args['data']))
                    if command == 'flash.read' and args_return_console(args) and os.environ.get('SIM_CONSOLE_EVENT') == '1':
                        await event('transport', detail='console opened')
                    await ws.send_json({'type': 'result', 'id': op['id'], 'state': 'completed', 'result': result})
                except Exception as error:
                    await ws.send_json({'type': 'result', 'id': op['id'], 'state': 'failed', 'error': {'code': getattr(error, 'code', 'simulated_failure')}})
                    if os.environ.get('SIM_SERIAL_FAIL_DURING_READ') == '1' and getattr(error, 'code', '') == 'serial_disconnected':
                        serial_lost = True                        # the page drops its agent shortly AFTER the result was acknowledged, then re-attaches the board
        gestures.cancel()
        for task in tasks:
            task.cancel()
    return reenumerate


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8080')
    parser.add_argument('--device', default='esp32-local')
    parser.add_argument('--grant', type=int, default=0, help='Explicit simulated local grant in seconds')
    a = parser.parse_args()
    asyncio.run(run(a.url.rstrip('/'), os.environ['BENCH_AGENT_TOKEN'], a.device, a.grant))

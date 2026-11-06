"""Real-process shutdown tests: long-lived connections must not delay the server's exit.

Isolated throw-away ports, state directories and random tokens; never the bench's own state.
Regression for Q3 (S3/UART): with a browser agent connected the server exceeded the launcher's
30 s stop timeout, so the launcher refused the replacement and exited 124.
"""
import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp import ClientSession, CookieJar, WSMsgType

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'server' / 'bench_server.py'
LAUNCHER = ROOT / 'tools' / 'run_server.py'
LIMIT = 15.0     # far below the launcher's 30 s default; the unfixed server needed ~121 s


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class Instance:
    def __init__(self, command, extra_env=None, **kwargs):
        self.work = tempfile.TemporaryDirectory()
        work = Path(self.work.name)
        (work / 'state').mkdir(mode=0o700)
        self.agent_token = secrets.token_urlsafe(32)
        (work / 'tokens.json').write_text(json.dumps({self.agent_token: {'id': 'a', 'role': 'agent', 'devices': ['board']}}))
        self.port = free_port()
        self.url = f'http://127.0.0.1:{self.port}'
        env = {**os.environ, 'BENCH_PORT': str(self.port), 'BENCH_STATE_DIR': str(work / 'state'),
               'BENCH_TOKENS_FILE': str(work / 'tokens.json'), 'BENCH_LOCK_TIMEOUT': '5', **(extra_env or {})}
        self.log = open(work / 'server.log', 'wb')
        self.process = subprocess.Popen(command, env=env, stdout=self.log, stderr=self.log, **kwargs)

    async def ready(self, session):
        for _ in range(150):
            try:
                async with session.get(self.url + '/health') as response:
                    if response.status == 200:
                        return
            except Exception:
                await asyncio.sleep(0.1)
        raise AssertionError('isolated server did not start')

    def children(self):
        text = Path(f'/proc/{self.process.pid}/task/{self.process.pid}/children').read_text().split()
        return [int(pid) for pid in text]

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.log.close()
        self.work.cleanup()


async def connect_agent(session, instance):
    ws = await session.ws_connect(instance.url + '/agent/v1/ws')
    await ws.send_json({'type': 'auth', 'version': 1, 'token': instance.agent_token})
    await ws.receive_json()
    await ws.send_json({'type': 'register', 'device': {'id': 'board', 'chip': 'esp32s3', 'flash_size': 4194304,
                                                       'capabilities': ['identify']}})
    await ws.receive_json()
    return ws


async def hold_like_the_page(session, instance):
    """What a real browser page holds: a registered agent WebSocket and an actively read UART event stream."""
    ws = await connect_agent(session, instance)
    events = {'ws': None, 'sse_ended': None}
    # like the page: a local browser session (cookie), then the UART event stream
    async with session.post(instance.url + '/session/v1/open', headers={'Origin': instance.url}) as opened:
        assert opened.status == 200, opened.status
    stream = await session.get(instance.url + '/api/v1/devices/board/events/stream?after=0', headers={'Origin': instance.url})
    assert stream.status == 200, stream.status

    async def read_ws():
        while True:
            message = await ws.receive()
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                events['ws'] = ws.close_code
                return

    async def read_stream():
        try:
            async for _ in stream.content.iter_any():
                pass
            events['sse_ended'] = True
        except Exception:
            events['sse_ended'] = 'error'
        finally:
            stream.close()
    tasks = [asyncio.create_task(read_ws()), asyncio.create_task(read_stream())]
    await asyncio.sleep(0.3)
    return events, tasks


async def wait_exit(process, limit):
    started = time.monotonic()
    while process.poll() is None and time.monotonic() - started < limit:
        await asyncio.sleep(0.05)
    return process.poll(), time.monotonic() - started


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        return ClientSession(cookie_jar=CookieJar(unsafe=True))

    async def test_idle_server_exits_immediately(self):
        instance = Instance([sys.executable, str(SERVER)])
        try:
            async with self.session() as session:
                await instance.ready(session)
            instance.process.send_signal(signal.SIGTERM)
            code, elapsed = await wait_exit(instance.process, LIMIT)
            self.assertEqual(code, 0)
            self.assertLess(elapsed, 3)
        finally:
            instance.close()

    async def test_connected_agent_and_event_stream_do_not_hold_the_server(self):
        instance = Instance([sys.executable, str(SERVER)])
        try:
            async with self.session() as session:
                await instance.ready(session)
                events, tasks = await hold_like_the_page(session, instance)
                instance.process.send_signal(signal.SIGTERM)
                code, elapsed = await wait_exit(instance.process, LIMIT)
                self.assertEqual(code, 0, f'server still running after {elapsed:.1f} s with an agent and a stream')
                self.assertLess(elapsed, 10)
                await asyncio.wait(tasks, timeout=3)
                self.assertEqual(events['ws'], 1001, 'agent must be told "going away", not cut off')
                self.assertTrue(events['sse_ended'])
        finally:
            instance.close()

    async def test_launcher_restart_with_a_connected_agent_stays_within_its_30_second_limit(self):
        """The Q3 incident: SIGHUP to the real launcher while the page's agent and stream are connected."""
        instance = Instance([sys.executable, str(LAUNCHER), '--shutdown-timeout', '30'], start_new_session=True)
        try:
            async with self.session() as session:
                await instance.ready(session)
                (old,) = instance.children()
                events, tasks = await hold_like_the_page(session, instance)
                before = await (await session.get(instance.url + '/health')).json()
                instance.process.send_signal(signal.SIGHUP)
                started = time.monotonic()
                after = None
                while time.monotonic() - started < 30 and after is None:
                    await asyncio.sleep(0.2)
                    try:
                        health = await (await session.get(instance.url + '/health')).json()
                        if health['epoch'] != before['epoch']:
                            after = health
                    except Exception:
                        pass
                self.assertIsNotNone(after, 'launcher did not replace the server within its stop timeout')
                self.assertIsNone(instance.process.poll(), 'launcher must still be running (it exits 124 when refusing)')
                self.assertNotIn(old, instance.children())
                self.assertEqual(events['ws'], 1001)
                for task in tasks:
                    task.cancel()
        finally:
            instance.close()


if __name__ == '__main__':
    unittest.main()

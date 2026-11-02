#!/usr/bin/env python3
"""Run with `python bench/server/bench_server.py`; configuration contains no implicit credentials."""
import asyncio
import hmac
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol import MAX_ARTIFACT, Fault, integer, require
from server.core import Broker

TOKENS = web.AppKey('tokens', dict)
BROKER = web.AppKey('broker', Broker)
ORIGIN = web.AppKey('origin', object)
IDENTITY = web.RequestKey('identity', dict)

PAGES = Path(__file__).resolve().parents[1] / 'web'


def credentials(config, token, role=None):
    require(isinstance(token, str) and 16 <= len(token) <= 512, 'unauthenticated', 'Invalid token', 401)
    for secret, identity in config.items():
        if hmac.compare_digest(token, secret) and (role is None or identity['role'] == role):
            return identity
    raise Fault('unauthenticated', 'Invalid token', 401)


def validate_config(config):
    require(isinstance(config, dict) and config, message='Configure at least one token')
    for secret, identity in config.items():
        require(isinstance(secret, str) and 16 <= len(secret) <= 512)
        require(isinstance(identity, dict) and isinstance(identity.get('id'), str))
        require(identity.get('role') in {'client', 'agent'})
        if identity['role'] == 'client':
            require(set(identity.get('scopes', [])) <= {'read', 'control', 'program'})
        else:
            require(isinstance(identity.get('devices'), list) and all(isinstance(x, str) for x in identity['devices']))


@web.middleware
async def errors(request, handler):
    try:
        # Same-origin browser calls only; CLI calls have no Origin.
        origin = request.headers.get('Origin')
        expected = request.app[ORIGIN] or f'{request.scheme}://{request.host}'
        require(not origin or origin == expected, 'origin_forbidden', 'Unexpected Origin', 403)
        if request.path.startswith('/api/v1/'):
            authorization = request.headers.get('Authorization', '')
            require(authorization.startswith('Bearer '), 'unauthenticated', 'Bearer token required', 401)
            identity = credentials(request.app[TOKENS], authorization[7:])
            request[IDENTITY] = identity
            if identity['role'] == 'client':
                scope = 'read' if request.method == 'GET' else 'control'
                require(scope in identity.get('scopes', []), 'forbidden', f'{scope} permission required', 403)
            else:
                require(request.path.startswith('/api/v1/artifacts'), 'forbidden', 'Agent REST access is artifact-only', 403)
        response = await handler(request)
    except Fault as e:
        response = web.json_response({'error': {'code': e.code, 'message': str(e)}}, status=e.status)
    except web.HTTPRequestEntityTooLarge:
        response = web.json_response({'error': {'code': 'body_too_large', 'message': 'Request too large'}}, status=413)
    except (ValueError, TypeError, KeyError, OverflowError):
        response = web.json_response({'error': {'code': 'invalid_request', 'message': 'Malformed request'}}, status=400)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self' https://unpkg.com; connect-src 'self' https://unpkg.com http:; "
        "style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
    return response


async def payload(request):
    require(request.content_length is None or request.content_length <= 65536, 'body_too_large', 'JSON body too large', 413)
    data = await request.read()
    require(len(data) <= 65536, 'body_too_large', 'JSON body too large', 413)
    value = json.loads(data)
    require(isinstance(value, dict))
    return value


def json_reply(value, status=200):
    return web.json_response(value, status=status)


async def health(request):
    return json_reply({'status': 'ok', 'protocol': 1, 'epoch': request.app[BROKER].epoch})


async def devices(request):
    b = request.app[BROKER]; b.sweep()
    return json_reply({'devices': [b.view(d) for d in b.devices.values()],
                       'agents': list(b.agents)})


async def leases(request):
    b, who = request.app[BROKER], request[IDENTITY]['id']
    b.sweep()
    if request.method == 'POST':
        data = await payload(request)
        return json_reply(b.reserve(data.get('device'), who, data.get('ttl', 300)), 201)
    key = request.match_info['id']
    if request.method == 'DELETE':
        b.lease(key, who); b.release(key, who)
        return json_reply({'released': True})
    lease = b.lease(key, who)
    if request.method == 'PATCH':
        data = await payload(request)
        from protocol import number
        ttl = number(data.get('ttl'), 1, 3600)
        lease['until'] = b.clock() + ttl
    return json_reply(lease)


async def operations(request):
    b, who = request.app[BROKER], request[IDENTITY]['id']
    b.sweep()
    if request.method == 'POST':
        data = await payload(request)
        result = b.submit(who, request[IDENTITY]['scopes'], data)
        return json_reply(result, 202)
    key = request.match_info['id']
    if request.method == 'DELETE':
        message = b.cancel(key, who)
        if message:
            op = b.operations[key]
            socket = b.agents.get(b.device(op['device'])['agent'])
            if socket and not socket.closed:
                await socket.send_json(message)
    return json_reply(b.operation(key, who))


async def events(request):
    after = int(request.query.get('after', '0'))
    integer(after, 0, 2**53 - 1)
    return json_reply(request.app[BROKER].events(request.match_info['device'], after))


async def event_stream(request):
    b = request.app[BROKER]
    device = request.match_info['device']
    b.device(device)
    after = int(request.query.get('after', '0'))
    integer(after, 0, 2**53 - 1)
    response = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store',
        'X-Accel-Buffering': 'no'})
    await response.prepare(request)
    try:
        while True:
            snapshot = b.events(device, after)
            await asyncio.wait_for(response.write(('data: ' + json.dumps(snapshot) + '\n\n').encode()), timeout=5)
            after = snapshot['next']
            await asyncio.sleep(.25)
    except (ConnectionError, asyncio.TimeoutError):
        pass
    return response


async def artifacts(request):
    b = request.app[BROKER]; b.sweep()
    if request.method == 'POST':
        data = await request.read()
        return json_reply(b.artifact(data, request.headers.get('X-SHA256')), 201)
    if 'id' not in request.match_info:
        require(request[IDENTITY]['role'] == 'client', 'forbidden', 'Client required', 403)
        return json_reply({'artifacts': [{k:v for k,v in a.items() if k != 'data'} for a in b.artifacts.values()]})
    key = request.match_info['id']
    a = b.artifacts.get(key)
    require(a is not None, 'not_found', 'Unknown or expired artifact', 404)
    identity = request[IDENTITY]
    if identity['role'] == 'agent':
        require(any(o['device'] in identity['devices'] and key in o['artifact_ids'] and o['state'] == 'running'
                    for o in b.operations.values()), 'forbidden', 'Artifact not assigned to this agent', 403)
    return web.Response(body=a['data'], content_type='application/octet-stream',
                        headers={'X-SHA256': a['sha256'], 'Content-Disposition': f'attachment; filename="{key}.bin"'})


async def websocket(request):
    b = request.app[BROKER]
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=65536)
    await ws.prepare(request)
    identity = None
    registered = False
    try:
        hello = await ws.receive_json(timeout=5)
        require(isinstance(hello, dict) and hello.get('type') == 'auth' and hello.get('version') == 1)
        identity = credentials(request.app[TOKENS], hello.get('token'), 'agent')
        b.agent(identity['id'], ws)
        registered = True
        await ws.send_json({'type': 'welcome', 'version': 1, 'epoch': b.epoch, 'server_ms': b.clock() * 1000})
        async for frame in ws:
            if frame.type != WSMsgType.TEXT:
                if frame.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                    break
                continue
            message = None
            try:
                message = json.loads(frame.data)
                require(isinstance(message, dict))
                kind = message.get('type')
                if kind == 'register':
                    generation = b.register(identity['id'], message.get('device'), identity['devices'])
                    await ws.send_json({'type': 'registered', 'device': message['device']['id'], 'generation': generation})
                elif kind == 'ready':
                    await ws.send_json(b.commit(identity['id'], message.get('id')))
                elif kind == 'result':
                    b.finish(identity['id'], message)
                else:
                    b.agent_event(identity['id'], message)
            except (Fault, ValueError, KeyError, TypeError) as e:
                await ws.send_json({'type': 'error', 'id': message.get('id') if isinstance(message, dict) else None,
                                    'error': {'code': getattr(e, 'code', 'invalid_message')}})
    except (Fault, ValueError, TypeError, KeyError, asyncio.TimeoutError):
        await ws.close(code=1008, message=b'Authentication or protocol error')
    finally:
        if registered:
            b.disconnect(identity['id'])
    return ws


async def dispatcher(app):
    async def run():
        sent_revocations = set()
        while True:
            await asyncio.sleep(0.05)
            b = app[BROKER]; b.sweep()
            sent_revocations.intersection_update(b.leases)
            for lease in list(b.leases.values()):
                if not lease['active'] and lease['id'] not in sent_revocations:
                    sent_revocations.add(lease['id'])
                    d = b.device(lease['device']); ws = b.agents.get(d['agent'])
                    if ws and not ws.closed:
                        try:
                            await ws.send_json({'type': 'lease_revoked', 'lease': lease['id']})
                        except (ConnectionError, RuntimeError):
                            b.disconnect(d['agent'])
            for d in list(b.devices.values()):
                message = b.offer(d['id'])
                if message:
                    ws = b.agents.get(d['agent'])
                    if ws and not ws.closed:
                        try:
                            await ws.send_json(message)
                        except (ConnectionError, RuntimeError):
                            b.disconnect(d['agent'])
    task = asyncio.create_task(run())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    for ws in list(app[BROKER].agents.values()):
        await ws.close(code=1001, message=b'Server stopping')


async def static(request):
    name = request.match_info.get('name') or 'index.html'
    require(name in {p.name for p in PAGES.iterdir() if p.is_file()}, 'not_found', 'Unknown asset', 404)
    return web.FileResponse(PAGES / name)


def create_app(tokens, broker=None, origin=None):
    validate_config(tokens)
    app = web.Application(middlewares=[errors], client_max_size=MAX_ARTIFACT)
    app[TOKENS], app[BROKER], app[ORIGIN] = tokens, broker or Broker(), origin
    app.cleanup_ctx.append(dispatcher)
    app.add_routes([web.get('/health', health), web.get('/agent/v1/ws', websocket),
                    web.get('/api/v1/devices', devices), web.post('/api/v1/leases', leases),
                    web.route('*', '/api/v1/leases/{id}', leases),
                    web.post('/api/v1/operations', operations), web.get('/api/v1/operations/{id}', operations),
                    web.delete('/api/v1/operations/{id}', operations),
                    web.get('/api/v1/devices/{device}/events', events),
                    web.get('/api/v1/devices/{device}/events/stream', event_stream),
                    web.get('/api/v1/artifacts', artifacts), web.post('/api/v1/artifacts', artifacts), web.get('/api/v1/artifacts/{id}', artifacts),
                    web.get('/', static), web.get('/{name}', static)])
    return app


if __name__ == '__main__':
    path = os.environ.get('BENCH_TOKENS_FILE')
    if not path:
        sys.exit('Set BENCH_TOKENS_FILE to an external JSON credentials file (see bench/README.md)')
    config = json.loads(Path(path).read_text())
    web.run_app(create_app(config, origin=os.environ.get('BENCH_ORIGIN')),
                host=os.environ.get('BENCH_HOST', '127.0.0.1'), port=int(os.environ.get('BENCH_PORT', 8080)),
                access_log=None)

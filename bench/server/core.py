"""Single-event-loop broker. No USB, project assertions, shell or persistent qualification logs."""
import copy
import hashlib
import json
import time
import uuid
from collections import deque
from protocol import (CAPABILITIES, MAX_ARTIFACT, PRIVILEGED, PROGRAM, Fault,
                      binary, integer, number, require, validate_command, local_origin)

TERMINAL = {'completed', 'failed', 'expired', 'cancelled', 'indeterminate'}


def uid():
    return uuid.uuid4().hex


class Broker:
    def __init__(self, clock=time.monotonic, max_events=2048):
        self.clock, self.max_events = clock, max_events
        self.agents, self.devices, self.leases, self.operations, self.artifacts = {}, {}, {}, {}, {}
        self.artifact_bytes = 0
        self.epoch = uid()

    def agent(self, agent_id, socket):
        require(agent_id not in self.agents, 'agent_connected', 'Agent already connected', 409)
        self.agents[agent_id] = socket

    def register(self, agent_id, data, allowed):
        require(isinstance(data, dict))
        device_id = data.get('id')
        require(isinstance(device_id, str) and device_id in allowed, 'device_forbidden', 'Device not allowed', 403)
        old = self.devices.get(device_id)
        require(not old or not old['connected'], 'device_connected', 'Device already connected', 409)
        caps = data.get('capabilities')
        require(isinstance(caps, list) and set(caps) <= CAPABILITIES)
        integer(data.get('flash_size'), 4096, 64 * 1024 * 1024)
        require(isinstance(data.get('chip'), str) and 1 <= len(data['chip']) <= 32)
        origins = data.get('http_origins', [])
        require(isinstance(origins, list) and len(origins) <= 8)
        origins = [local_origin(origin) for origin in origins]
        generation = uid()
        self.devices[device_id] = {
            'id': device_id, 'agent': agent_id, 'generation': generation, 'connected': True,
            'http_origins': origins, 'chip': data['chip'], 'flash_size': data['flash_size'], 'capabilities': caps,
            'grant_until': 0, 'quarantined': bool(old and old['quarantined']) or data.get('hold') is True,
            'events': old['events'] if old else deque(maxlen=self.max_events),
            'seq': old['seq'] if old else 0, 'source_seq': 0,
        }
        self.event(device_id, 'connected', {'generation': generation})
        return generation

    def view(self, d):
        reservation = next((lease for lease in self.leases.values() if lease['device'] == d['id'] and lease['active'] and lease['until'] > self.clock()), None)
        return {k: v for k, v in d.items() if k not in {'events', 'grant_until', 'source_seq'}} | {
            'privileged_seconds': max(0, d['grant_until'] - self.clock()),
            'reservation': {'owner': reservation['owner'], 'remaining_seconds': max(0, reservation['until'] - self.clock())} if reservation else None}

    def event(self, device, kind, payload):
        d = self.devices[device]
        d['seq'] += 1
        d['events'].append({'seq': d['seq'], 'epoch': self.epoch, 'at': time.time(), 'kind': kind, **payload})

    def events(self, device, after):
        d = self.device(device)
        first = d['events'][0]['seq'] if d['events'] else d['seq'] + 1
        events, size = [], 0
        for event in d['events']:
            if event['seq'] <= after:
                continue
            event_size = len(json.dumps(event).encode())
            if events and size + event_size > 48 * 1024:
                break
            events.append(event); size += event_size
        return {'epoch': self.epoch, 'next': events[-1]['seq'] if events else d['seq'],
                'latest': d['seq'], 'lost': max(0, first - after - 1), 'events': events}

    def device(self, device):
        require(device in self.devices, 'not_found', 'Unknown device', 404)
        return self.devices[device]

    def sweep(self):
        now = self.clock()
        for lease in list(self.leases.values()):
            if lease['active'] and lease['until'] <= now:
                self.release(lease['id'], lease['owner'], 'lease_expired')
        for op in self.operations.values():
            if op['state'] not in TERMINAL and op['deadline'] <= now:
                if op['state'] == 'running':
                    op['state'], op['error'] = 'indeterminate', {'code': 'execution_timeout'}
                    self.device(op['device'])['quarantined'] = True
                else:
                    op['state'], op['error'] = 'expired', {'code': 'command_expired'}
        # Retain outcomes for one hour, never evict active or unsettled operations.
        for key, op in list(self.operations.items()):
            if op['state'] in TERMINAL and now - op['created'] > 3600 and not op.get('unsettled'):
                del self.operations[key]
        for key, lease in list(self.leases.items()):
            if not lease['active'] and now - lease['until'] > 3600:
                del self.leases[key]
        for key, art in list(self.artifacts.items()):
            if now - art['created'] > 3600 and not any(
                op['state'] not in TERMINAL and key in op['artifact_ids'] for op in self.operations.values()
            ):
                self.artifact_bytes -= art['size']
                del self.artifacts[key]

    def reserve(self, device, owner, ttl):
        self.sweep()
        d = self.device(device)
        require(d['connected'], 'device_unavailable', 'Device offline', 409)
        require(not any(x['active'] and x['device'] == device for x in self.leases.values()),
                'device_reserved', 'Device already reserved', 409)
        number(ttl, 1, 3600)
        require(len(self.leases) < 4096, 'capacity', 'Reservation store full', 503)
        lease = {'id': uid(), 'device': device, 'owner': owner, 'until': self.clock() + ttl, 'active': True}
        self.leases[lease['id']] = lease
        return lease.copy()

    def lease(self, key, owner):
        lease = self.leases.get(key)
        require(lease is not None, 'not_found', 'Unknown reservation', 404)
        require(lease['owner'] == owner, 'forbidden', 'Reservation belongs to another client', 403)
        require(lease['active'] and lease['until'] > self.clock(), 'lease_expired', 'Reservation expired', 409)
        return lease

    def release(self, key, owner, reason='released'):
        lease = self.leases.get(key)
        require(lease is not None and lease['owner'] == owner, 'forbidden', 'Invalid reservation', 403)
        lease['active'] = False
        for op in self.operations.values():
            if op['lease'] == key and op['state'] not in TERMINAL:
                if op['state'] == 'running':
                    op['state'] = 'indeterminate'
                    self.device(op['device'])['quarantined'] = True
                else:
                    op['state'] = 'cancelled'
                op['error'] = {'code': reason}

    def artifact(self, data, expected):
        require(0 < len(data) <= MAX_ARTIFACT, 'artifact_size', 'Invalid artifact size', 413)
        digest = hashlib.sha256(data).hexdigest()
        require(digest == expected, 'digest_mismatch', 'SHA-256 mismatch')
        require(self.artifact_bytes + len(data) <= 64 * 1024 * 1024, 'capacity', 'Artifact store full', 503)
        a = {'id': uid(), 'data': data, 'size': len(data), 'sha256': digest, 'created': self.clock()}
        self.artifacts[a['id']] = a
        self.artifact_bytes += len(data)
        return {k: v for k, v in a.items() if k != 'data'}

    def submit(self, owner, scopes, body):
        self.sweep()
        require(isinstance(body, dict) and not (body.keys() - {'lease', 'command', 'args', 'ttl', 'timeout', 'allow_destructive'}))
        lease = self.lease(body.get('lease'), owner)
        d = self.device(lease['device'])
        require(d['connected'], 'device_unavailable', 'Device offline', 409)
        require(not d['quarantined'] or body.get('command') in {'identify', 'partitions', 'flash.read', 'mark'},
                'device_quarantined', 'Read back and recover locally before control or programming', 409)
        name, args = body.get('command'), body.get('args', {})
        require('control' in scopes, 'forbidden', 'Control permission required', 403)
        validate_command(name, args, d, self.artifacts)
        if name in PRIVILEGED:
            require(d['grant_until'] > self.clock(), 'grant_required', 'Local operator grant required', 403)
        if name in PROGRAM:
            require('program' in scopes and body.get('allow_destructive') is True,
                    'destructive_not_authorized', 'Explicit programming permission and consent required', 403)
        require(len(self.operations) < 4096, 'capacity', 'Operation store full', 503)
        ttl, timeout = body.get('ttl', 30), body.get('timeout', 120)
        number(ttl, 0.1, 120); number(timeout, 0.1, 600)
        key = uid()
        op = {'id': key, 'owner': owner, 'lease': lease['id'], 'device': d['id'], 'generation': d['generation'],
              'command': name, 'args': copy.deepcopy(args), 'state': 'queued', 'created': self.clock(),
              'deadline': self.clock() + ttl, 'timeout': timeout, 'error': None, 'result': None,
              'cancel_requested': False, 'unsettled': False,
              'artifact_ids': [i['artifact'] for i in args.get('images', [])]}
        self.operations[key] = op
        return self.operation(key, owner)

    def operation(self, key, owner):
        require(key in self.operations, 'not_found', 'Unknown operation', 404)
        op = self.operations[key]
        require(op['owner'] == owner, 'forbidden', 'Operation belongs to another client', 403)
        # UART transmit bytes are never returned in diagnostics.
        return {k: copy.deepcopy(v) for k, v in op.items() if k not in {'args', 'artifact_ids', 'deadline', 'created'}}

    def offer(self, device):
        self.sweep()
        d = self.device(device)
        if not d['connected']:
            return None
        if any(o['device'] == device and (o['state'] in {'offered', 'running'} or o['unsettled']) for o in self.operations.values()):
            return None
        for op in self.operations.values():
            if op['device'] == device and op['state'] == 'queued':
                if d['quarantined'] and op['command'] not in {'identify', 'partitions', 'flash.read', 'mark'}:
                    op['state'], op['error'] = 'cancelled', {'code': 'device_quarantined'}
                    continue
                op['state'] = 'offered'
                return {'type': 'offer', 'id': op['id'], 'device': device, 'generation': d['generation'],
                        'command': op['command'], 'args': op['args'], 'lease': op['lease'],
                        'lease_deadline_ms': self.leases[op['lease']]['until'] * 1000,
                        'deadline_ms': op['deadline'] * 1000, 'remaining_ms': max(0, (op['deadline'] - self.clock()) * 1000),
                        'timeout_ms': op['timeout'] * 1000}
        return None

    def commit(self, agent, key):
        self.sweep()
        op = self.operations.get(key)
        require(op is not None, 'not_found', 'Unknown operation', 404)
        d = self.device(op['device'])
        require(d['agent'] == agent and d['generation'] == op['generation'], 'stale_agent', 'Agent generation changed', 409)
        require(not d['quarantined'] or op['command'] in {'identify', 'partitions', 'flash.read', 'mark'},
                'device_quarantined', 'Device requires recovery', 409)
        require(op['state'] == 'offered', 'not_executable', 'Command no longer executable', 409)
        self.lease(op['lease'], op['owner'])
        if op['command'] in PRIVILEGED:
            require(d['grant_until'] > self.clock(), 'grant_required', 'Grant expired', 403)
        op['start_remaining_ms'] = max(0, (op['deadline'] - self.clock()) * 1000)
        op['state'], op['unsettled'] = 'running', True
        op['deadline'] = self.clock() + op['timeout']
        return {'type': 'execute', 'id': key, 'remaining_ms': op['start_remaining_ms'],
                'deadline_ms': self.clock() * 1000 + op['start_remaining_ms'],
                'execution_deadline_ms': op['deadline'] * 1000}

    def finish(self, agent, message):
        # Results can arrive between dispatcher ticks: enforce the deadline before accepting them.
        self.sweep()
        op = self.operations.get(message.get('id'))
        require(op is not None, 'not_found', 'Unknown operation', 404)
        d = self.device(op['device'])
        require(d['agent'] == agent and op['generation'] == d['generation'], 'stale_agent', 'Stale result', 409)
        require(op['unsettled'], 'duplicate_result', 'No execution awaiting a result', 409)
        state = message.get('state')
        require(state in {'completed', 'failed', 'indeterminate', 'cancelled'})
        error = message.get('error')
        require(error is None or isinstance(error, dict) and isinstance(error.get('code'), str),
                'invalid_result', 'Errors must be structured')
        # Timeouts/cancel requests never turn into a fabricated success. Late evidence remains inspectable.
        op['unsettled'] = False
        if op['state'] == 'running':
            op['state'] = state
            op['result'], op['error'] = message.get('result'), message.get('error')
        else:
            op['late_result'] = {'state': state, 'result': message.get('result'), 'error': message.get('error')}
        if state == 'indeterminate' or message.get('hold') is True:
            d['quarantined'] = True
        self.event(d['id'], 'operation', {'id': op['id'], 'state': op['state']})
        return {'type': 'result_ack', 'id': op['id'], 'device': d['id'], 'generation': d['generation'],
                'state': op['state'], 'quarantined': d['quarantined']}

    def cancel(self, key, owner):
        op = self.operations.get(key)
        self.operation(key, owner)
        if op['state'] == 'running':
            op['cancel_requested'] = True
            # No false promise that esptool can interrupt safely.
            return {'type': 'cancel', 'id': key}
        if op['state'] not in TERMINAL:
            op['state'] = 'cancelled'
        return None

    def disconnect(self, agent):
        self.agents.pop(agent, None)
        for d in self.devices.values():
            if d['agent'] != agent or not d['connected']:
                continue
            d['connected'], d['grant_until'] = False, 0
            self.event(d['id'], 'disconnected', {})
            for lease in list(self.leases.values()):
                if lease['device'] == d['id'] and lease['active']:
                    self.release(lease['id'], lease['owner'], 'agent_disconnected')
            for op in self.operations.values():
                if op['device'] == d['id']:
                    op['unsettled'] = False  # device quarantine, not a replay, governs future recovery

    def agent_event(self, agent, message):
        d = self.device(message.get('device'))
        require(d['agent'] == agent and d['connected'] and message.get('generation') == d['generation'],
                'stale_agent', 'Invalid agent device', 409)
        kind = message.get('type')
        if kind == 'grant':
            seconds = number(message.get('seconds'), 0, 14400)
            d['grant_until'] = self.clock() + seconds
            self.event(d['id'], 'grant', {'seconds': seconds})
        elif kind == 'hold':
            d['quarantined'] = True
            self.event(d['id'], 'quarantined', {})
        elif kind == 'recover':
            require(not any(o['device'] == d['id'] and o['unsettled'] for o in self.operations.values()),
                    'still_running', 'Hardware execution is still unsettled', 409)
            d['quarantined'] = False
            self.event(d['id'], 'recovered', {})
        elif kind == 'uart':
            seq = integer(message.get('seq'), 1, 2**53 - 1)
            require(seq > d['source_seq'], 'replayed_event', 'Duplicate UART event', 409)
            binary(message.get('data'))
            lost = seq - d['source_seq'] - 1
            d['source_seq'] = seq
            self.event(d['id'], 'uart', {'data': message['data'], 'source_seq': seq,
                                       'source_lost': lost, 'agent_at': message.get('at')})
        elif kind in {'mark', 'transport', 'trigger'}:
            self.event(d['id'], kind, {'detail': str(message.get('detail', ''))[:256]})
        else:
            raise Fault('invalid_event', 'Unknown agent event')

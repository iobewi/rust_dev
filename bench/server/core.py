"""Single-event-loop broker. No USB, project assertions, shell or persistent qualification logs."""
import copy
import hashlib
import json
import time
import uuid
from functools import wraps
from collections import deque
from protocol import (CAPABILITIES, MAX_ARTIFACT, PROGRAM, Fault,
                      binary, integer, number, require, validate_command, local_origin,
                      trigger_disarm, requires_grant)

TERMINAL = {'completed', 'failed', 'expired', 'cancelled', 'indeterminate'}


def uid():
    return uuid.uuid4().hex


def checkpoint(method):
    @wraps(method)
    def durable(self, *args, **kwargs):
        require(not self.journal_failed, 'journal_unavailable', 'Durable state unavailable; restart required', 503)
        try:
            return method(self, *args, **kwargs)
        finally:
            if self.journal:
                try:
                    self.journal.save(self)
                except Exception:
                    self.journal_failed = True
                    raise Fault('journal_unavailable', 'Durable checkpoint failed; no execution acknowledgement', 503) from None
    return durable


class Broker:
    def __init__(self, clock=time.monotonic, max_events=2048, state_dir=None, lock_timeout=0):
        self.clock, self.max_events = clock, max_events
        self.agents, self.devices, self.leases, self.operations, self.artifacts = {}, {}, {}, {}, {}
        self.artifact_bytes = 0
        self.upload_reserved = 0
        self.epoch = uid()
        self.journal, self.journal_failed = None, False
        if state_dir is not None:
            from server.journal import Journal
            self.journal = Journal(state_dir, lock_timeout=lock_timeout)
            state, self.artifacts = self.journal.load()
            self.artifact_bytes = sum(a['size'] for a in self.artifacts.values())
            require(self.artifact_bytes <= 64 * 1024 * 1024, 'capacity', 'Durable store exceeds capacity', 503)
            self.devices, self.operations, self.leases = state['devices'], state['operations'], state['leases']
            for lease in self.leases.values():
                lease['active'], lease['until'] = False, self.clock()
            for d in self.devices.values():
                d.update(connected=False, grant_until=0, source_seq=0, seq=0, events=deque(maxlen=max_events))
            for a in self.artifacts.values():
                a['created'] = self.clock()
            for op in self.operations.values():
                op['created'], op['deadline'] = self.clock(), 0
                if op['state'] in {'queued', 'offered'}:
                    op['state'], op['error'] = 'cancelled', {'code': 'server_restart_before_execution'}
                elif op['state'] == 'running':
                    op['state'], op['error'] = 'indeterminate', {'code': 'server_restart'}
                if op['state'] == 'indeterminate' and not op.get('reconciled') or op.get('requires_settle') and not op.get('settled'):
                    op['reconciliation_required'] = True
                    self.devices[op['device']]['quarantined'] = True
                op['unsettled'] = False  # Prior generation cannot report/settle. Local reconciliation only.
            self.journal.save(self)

    def agent(self, agent_id, socket):
        require(agent_id not in self.agents, 'agent_connected', 'Agent already connected', 409)
        self.agents[agent_id] = socket

    @checkpoint
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
        provenance = data.get('provenance', {})
        require(isinstance(provenance, dict) and not provenance.keys() - {
            'usb_vid', 'usb_pid', 'transport', 'transport_basis', 'agent_sha256', 'assets_sha256'})
        for key in ('usb_vid', 'usb_pid'):
            if provenance.get(key) is not None:
                integer(provenance[key], 0, 65535)
        for key in ('agent_sha256', 'assets_sha256'):
            value = provenance.get(key)
            require(value is None or isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value))
        require(provenance.get('transport', 'unknown') in {'unknown', 'external_uart', 'usb_serial_jtag', 'native_usb_other'})
        require(provenance.get('transport_basis', 'unavailable') in {'unavailable', 'inferred_from_webserial_vid_pid'})
        generation = uid()
        self.devices[device_id] = {
            'id': device_id, 'agent': agent_id, 'generation': generation, 'connected': True,
            'provenance': copy.deepcopy(provenance), 'provenance_source': 'agent_reported',
            'http_origins': origins, 'chip': data['chip'], 'flash_size': data['flash_size'], 'capabilities': caps,
            'grant_until': 0, 'recovered_at': None, 'quarantined': bool(old and old['quarantined']) or data.get('hold') is True,
            'events': old['events'] if old else deque(maxlen=self.max_events),
            'seq': old['seq'] if old else 0, 'source_seq': 0,
        }
        self.event(device_id, 'connected', {'generation': generation})
        return generation

    def view(self, d):
        reservation = next((lease for lease in self.leases.values() if lease['device'] == d['id'] and lease['active'] and lease['until'] > self.clock()), None)
        return {k: v for k, v in d.items() if k not in {'events', 'grant_until', 'source_seq', 'trigger_lease'}} | {
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

    @checkpoint
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
        self.release_automatic()
        # Retain outcomes for one hour, never evict active or unsettled operations.
        for key, op in list(self.operations.items()):
            if op['state'] in TERMINAL and now - op['created'] > 3600 and not self.operation_pins(op):
                del self.operations[key]
        for key, lease in list(self.leases.items()):
            if not lease['active'] and now - lease['until'] > 3600 and not any(
                    op['lease'] == key and self.operation_pins(op) for op in self.operations.values()):
                del self.leases[key]
        for key, art in list(self.artifacts.items()):
            if now - art['created'] > 3600 and not self.artifact_pinned(key):
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

    @checkpoint
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

    def release_automatic(self):
        for lease in list(self.leases.values()):
            if not lease['active'] or not lease.get('automatic') or lease.get('keep_until', 0) > self.clock():
                continue
            ops = [op for op in self.operations.values() if op['lease'] == lease['id']]
            if ops and all(op['state'] in TERMINAL and not op['unsettled'] and
                           (not op['requires_settle'] or op['settled']) for op in ops):
                self.release(lease['id'], lease['owner'])

    def manual(self, owner, scopes, body, local_agent=None):
        self.sweep()
        require(isinstance(body, dict) and not body.keys() - {'device', 'generation', 'command', 'args', 'ttl', 'timeout', 'allow_destructive'})
        d = self.device(body.get('device'))
        require(body.get('generation') == d['generation'], 'device_changed', 'Device changed; confirm the target again', 409)
        lease = next((x for x in self.leases.values() if x['active'] and x['device'] == d['id']), None)
        require(not lease or lease['owner'] == owner and lease.get('automatic'), 'device_reserved', 'Device already reserved', 409)
        created = lease is None
        if created:
            lease = self.leases[self.reserve(d['id'], owner, 900)['id']]
            lease['automatic'] = True
        else:
            lease['until'] = self.clock() + 900
        try:
            return self.submit(owner, scopes, {k: v for k, v in body.items() if k not in {'device', 'generation'}} | {'lease': lease['id']}, local_agent)
        except Exception:
            if created:
                self.release(lease['id'], owner)
            raise

    @staticmethod
    def operation_pins(op):
        return (op['state'] not in TERMINAL or op.get('unsettled') or
                op.get('requires_settle') and not op.get('settled') or
                op['state'] == 'indeterminate' and not op.get('reconciled'))

    def artifact_pinned(self, key):
        artifact = self.artifacts[key]
        for op in self.operations.values():
            if not self.operation_pins(op):
                continue
            references = set(op['artifact_ids'])
            for result in [op.get('result'), (op.get('late_result') or {}).get('result')]:
                if isinstance(result, dict) and isinstance(result.get('artifact'), dict):
                    references.add(result['artifact'].get('id'))
            device = self.devices.get(op['device'], {})
            if key in references or artifact.get('owner') == 'agent:' + op.get('agent', device.get('agent', '')):
                return True
        return False

    def storage(self):
        return {'total': 64 * 1024 * 1024, 'used': self.artifact_bytes,
                'reserved': self.upload_reserved,
                'available': 64 * 1024 * 1024 - self.artifact_bytes - self.upload_reserved,
                'max_artifact': MAX_ARTIFACT}

    @checkpoint
    def delete_artifact(self, key, owner):
        artifact = self.artifacts.get(key)
        require(artifact is not None, 'not_found', 'Unknown artifact', 404)
        # Client uploads belong to their uploader. Agent readbacks may be cleaned
        # by the client owning the operation which produced them.
        owns_result = any(op['owner'] == owner and
            artifact.get('owner') == 'agent:' + op.get('agent', self.devices.get(op['device'], {}).get('agent', '')) and
            isinstance(op.get('result'), dict) and
            (op['result'].get('artifact') or {}).get('id') == key for op in self.operations.values())
        require(artifact.get('owner') == 'client:' + owner or owns_result,
                'forbidden', 'Artifact belongs to another owner', 403)
        require(not self.artifact_pinned(key), 'artifact_in_use', 'Artifact retained for execution or recovery', 409)
        self.artifact_bytes -= artifact['size']
        del self.artifacts[key]
        return {'deleted': True}

    @checkpoint
    def artifact(self, data, expected, owner=None):
        require(0 < len(data) <= MAX_ARTIFACT, 'artifact_size', 'Invalid artifact size', 413)
        digest = hashlib.sha256(data).hexdigest()
        require(digest == expected, 'digest_mismatch', 'SHA-256 mismatch')
        require(len(data) <= self.storage()['available'], 'capacity', 'Artifact store full', 503)
        a = {'id': uid(), 'data': data, 'size': len(data), 'sha256': digest, 'created': self.clock(), 'owner': owner}
        self.artifacts[a['id']] = a
        self.artifact_bytes += len(data)
        return {k: v for k, v in a.items() if k != 'data'}

    @checkpoint
    def submit(self, owner, scopes, body, local_agent=None):
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
        local_control = local_agent is not None and local_agent == d['agent']
        if trigger_disarm(name, args) and not local_control:
            require(d.get('trigger_lease') == lease['id'], 'trigger_not_owned',
                    'Trigger belongs to another reservation or generation', 403)
        if requires_grant(name, args) and not local_control:
            require(d['grant_until'] > self.clock(), 'grant_required', 'Local operator grant required', 403)
        if name in PROGRAM:
            require('program' in scopes, 'program_permission_required', 'Programming permission required', 403)
            require(body.get('allow_destructive') is True, 'destructive_consent_required', 'Explicit programming consent required', 403)
        require(len(self.operations) < 4096, 'capacity', 'Operation store full', 503)
        ttl, timeout = body.get('ttl', 30), body.get('timeout', 120)
        number(ttl, 0.1, 120); number(timeout, 0.1, 600)
        key = uid()
        op = {'id': key, 'owner': owner, 'lease': lease['id'], 'device': d['id'], 'generation': d['generation'],
              'epoch': self.epoch, 'agent': d['agent'], 'command': name, 'args': copy.deepcopy(args), 'state': 'queued', 'created': self.clock(),
              'deadline': self.clock() + ttl, 'timeout': timeout, 'error': None, 'result': None,
              'cancel_requested': False, 'unsettled': False, 'local_control': local_control, 'requires_settle': False, 'settled': False,
              'artifact_ids': [i['artifact'] for i in args.get('images', [])]}
        self.operations[key] = op
        return self.operation(key, owner)

    def operation(self, key, owner):
        require(key in self.operations, 'not_found', 'Unknown operation', 404)
        op = self.operations[key]
        require(op['owner'] == owner, 'forbidden', 'Operation belongs to another client', 403)
        # UART transmit bytes are never returned in diagnostics.
        return {k: copy.deepcopy(v) for k, v in op.items() if k not in {'args', 'artifact_ids', 'deadline', 'created'}}

    @checkpoint
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
                        'command': op['command'], 'args': op['args'], 'lease': op['lease'], 'local_control': op['local_control'],
                        'lease_deadline_ms': self.leases[op['lease']]['until'] * 1000,
                        'deadline_ms': op['deadline'] * 1000, 'remaining_ms': max(0, (op['deadline'] - self.clock()) * 1000),
                        'timeout_ms': op['timeout'] * 1000}
        return None

    @checkpoint
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
        if trigger_disarm(op['command'], op['args']) and not op['local_control']:
            require(d.get('trigger_lease') == op['lease'], 'trigger_not_owned',
                    'Trigger belongs to another reservation or generation', 403)
        if requires_grant(op['command'], op['args']) and not op['local_control']:
            require(d['grant_until'] > self.clock(), 'grant_required', 'Grant expired', 403)
        op['start_remaining_ms'] = max(0, (op['deadline'] - self.clock()) * 1000)
        op['state'], op['unsettled'], op['requires_settle'] = 'running', True, True
        op['deadline'] = self.clock() + op['timeout']
        return {'type': 'execute', 'id': key, 'remaining_ms': op['start_remaining_ms'],
                'deadline_ms': self.clock() * 1000 + op['start_remaining_ms'],
                'execution_deadline_ms': op['deadline'] * 1000}

    @checkpoint
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
            if state == 'completed' and op['command'] == 'trigger':
                d['trigger_lease'] = None if trigger_disarm(op['command'], op['args']) else op['lease']
        else:
            op['late_result'] = {'state': state, 'result': message.get('result'), 'error': message.get('error')}
        if state == 'indeterminate' or message.get('hold') is True:
            d['quarantined'] = True
        self.event(d['id'], 'operation', {'id': op['id'], 'state': op['state']})
        return {'type': 'result_ack', 'id': op['id'], 'device': d['id'], 'generation': d['generation'],
                'state': op['state'], 'quarantined': d['quarantined']}

    @checkpoint
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

    @checkpoint
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
                    op['unsettled'] = False  # prior agent cannot settle in a new generation
                    if op.get('requires_settle') and not op.get('settled'):
                        op['reconciliation_required'] = True
                        d['quarantined'] = True

    @checkpoint
    def agent_event(self, agent, message):
        d = self.device(message.get('device'))
        require(d['agent'] == agent and d['connected'] and message.get('generation') == d['generation'],
                'stale_agent', 'Invalid agent device', 409)
        kind = message.get('type')
        if kind == 'settled':
            op = self.operations.get(message.get('id'))
            require(op is not None and op['device'] == d['id'] and op['generation'] == d['generation'] and
                    op['state'] in TERMINAL and not op['unsettled'] and op['requires_settle'] and not op['settled'],
                    'not_settleable', 'No acknowledged result to settle', 409)
            op['settled'] = True
            if op['command'] == 'trigger' and op['state'] == 'completed':
                self.leases[op['lease']]['keep_until'] = (self.clock() + op['args']['expires_ms'] / 1000
                    if op['args'].get('action') != 'disarm' else 0)
            self.release_automatic()
        elif kind == 'grant':
            seconds = number(message.get('seconds'), 0, 14400)
            d['grant_until'] = self.clock() + seconds
            self.event(d['id'], 'grant', {'seconds': seconds})
        elif kind == 'release_control':
            # Only the authenticated physical agent can release abandoned control.
            # Refuse while hardware or its result is unsettled, including timed-out work.
            require(not any(o['device'] == d['id'] and (o['unsettled'] or o['state'] == 'running' or
                    self.leases[o['lease']].get('automatic') and o['requires_settle'] and not o['settled'])
                            for o in self.operations.values()),
                    'still_running', 'Wait for hardware completion before releasing control', 409)
            d['grant_until'] = 0
            for lease in list(self.leases.values()):
                if lease['device'] == d['id'] and lease['active']:
                    self.release(lease['id'], lease['owner'], 'operator_released')
            self.event(d['id'], 'control_released', {})
        elif kind == 'hold':
            d['quarantined'] = True
            self.event(d['id'], 'quarantined', {})
        elif kind == 'recover':
            require(not any(o['device'] == d['id'] and o['unsettled'] for o in self.operations.values()),
                    'still_running', 'Hardware execution is still unsettled', 409)
            d['quarantined'] = False
            d['recovered_at'] = time.time()
            for op in self.operations.values():
                if op['device'] == d['id'] and (op['state'] == 'indeterminate' or op.get('reconciliation_required')):
                    op['reconciled'] = True
                    op['reconciliation_required'] = False
                    op['reconciled_at'] = time.time()
                    op['reconciled_generation'] = d['generation']
                    op['settled'] = True
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

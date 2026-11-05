"""Private, synchronous operation checkpoint. No hardware commands are replayed."""
import fcntl
import hashlib
import json
import os
import math
import sys
import time
from pathlib import Path
import sqlite3

# Keep flash verification and artifact metadata, never UART, MACs, HTTP bodies or
# arbitrary loader error strings. A digest identifies the complete original result.
SAFE_FIELDS = set('artifact id size sha256 address chip flash_size flash_id erased written '
    'ok state flash verification transfer md5 sha256_readback items addr region regions '
    'begin requires_check hold failed_at mode reset method boot_verified baudrate sent '
    'duration_ms armed simulated verified http_status response'.split())


def safe_result(value):
    if isinstance(value, dict):
        return {k: safe_result(v) for k, v in value.items() if k in SAFE_FIELDS}
    if isinstance(value, list):
        return [safe_result(v) for v in value[:256]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:256]


class Journal:
    def __init__(self, directory, *, lock_timeout=0):
        if type(lock_timeout) not in (int, float) or not math.isfinite(lock_timeout) or not 0 <= lock_timeout <= 600:
            raise ValueError('Lock timeout must be between 0 and 600 seconds')
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # One authoritative process; prevent an accidental second writer.
        self.lock = (directory / 'server.lock').open('a')
        os.chmod(directory / 'server.lock', 0o600)
        try:
            deadline = time.monotonic() + lock_timeout
            waiting = False
            while True:
                try:
                    fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if waiting:
                        print('Durable journal lock acquired; starting server.', file=sys.stderr, flush=True)
                    break
                except BlockingIOError:
                    if not waiting:
                        print('Durable journal locked by another process; waiting up to '
                              f'{lock_timeout:g}s. Lock and journal are preserved.', file=sys.stderr, flush=True)
                        waiting = True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BlockingIOError('Durable journal still locked; startup refused after '
                                              f'{lock_timeout:g}s. Stop the previous server and wait for its exit.') from None
                    time.sleep(min(.1, remaining))
        except BaseException:
            self.lock.close()
            raise
        path = directory / 'operations.sqlite3'
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, meta TEXT NOT NULL, data BLOB NOT NULL)')
        self.db.commit()
        self.last_body = None
        self.saved_ids = set()

    def load(self):
        row = self.db.execute('SELECT body FROM checkpoint WHERE id=1').fetchone()
        state = json.loads(row[0]) if row else {'devices': {}, 'operations': {}, 'leases': {}}
        artifacts = {}
        for key, meta, data in self.db.execute('SELECT id,meta,data FROM artifacts'):
            artifact = json.loads(meta)
            if len(data) != artifact['size'] or hashlib.sha256(data).hexdigest() != artifact['sha256']:
                raise ValueError('Durable artifact integrity failure')
            artifacts[key] = {**artifact, 'data': data}
        self.saved_ids = set(artifacts)
        return state, artifacts

    def save(self, broker):
        devices = {key: {k: v for k, v in d.items() if k not in {'events', 'grant_until', 'source_seq', 'seq'}}
                   for key, d in broker.devices.items()}
        operations = {}
        for key, op in broker.operations.items():
            record = {k: v for k, v in op.items() if k not in {'args', 'result', 'late_result', 'error'}}
            record['args'] = {}  # Never enough information to replay an operation.
            record['result'] = safe_result(op.get('result'))
            if op.get('result') is not None:
                record['result_sha256'] = op.get('result_sha256') or hashlib.sha256(
                    json.dumps(op['result'], sort_keys=True).encode()).hexdigest()
            for field in ['error', 'late_result']:
                value = op.get(field)
                if field == 'error':
                    record[field] = {k: v for k, v in (value or {}).items()
                                     if k in {'code', 'phase', 'http_status', 'received_bytes', 'expected_bytes'}} or None
                elif value is not None:
                    record[field] = {'state': value['state'], 'result': safe_result(value.get('result'))}
            operations[key] = record
        body = json.dumps({'devices': devices, 'operations': operations, 'leases': broker.leases})
        if body == self.last_body and set(broker.artifacts) == self.saved_ids:
            return
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO checkpoint VALUES (1,?)', (body,))
            for key, artifact in broker.artifacts.items():
                if key in self.saved_ids:
                    continue
                self.db.execute('INSERT OR IGNORE INTO artifacts VALUES (?,?,?)',
                    (key, json.dumps({k: v for k, v in artifact.items() if k != 'data'}), artifact['data']))
            for (key,) in self.db.execute('SELECT id FROM artifacts').fetchall():
                if key not in broker.artifacts:
                    self.db.execute('DELETE FROM artifacts WHERE id=?', (key,))

        self.last_body = body
        self.saved_ids = set(broker.artifacts)

    def close(self):
        self.db.close()
        self.lock.close()

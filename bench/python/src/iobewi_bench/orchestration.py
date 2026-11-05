"""Small explicit Python primitives for pytest recipes, without a recipe language."""
import hashlib
import json
import os
from pathlib import Path

from .client import BenchError, _duration


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_file(stream):
    stream.flush()
    os.fsync(stream.fileno())


class Orchestrator:
    def __init__(self, campaign):
        if campaign.manifest is None:
            raise ValueError('A validated campaign manifest is required')
        self.campaign = campaign


    def manual_window(self, label, *, interruption=False):
        if interruption:
            if self.campaign.manual_interruptions >= self.campaign.manifest.document['max_interruptions']:
                raise ValueError('Intentional interruption budget exhausted')
            self.campaign.manual_interruptions += 1
        self.campaign.client.record('manual-windows.jsonl', {'label':label,
            'interruption':interruption,'count':self.campaign.manual_interruptions})
        print('Operator action window: ' + label, flush=True)

    def wait_ready(self, *, timeout=120, grant_seconds=0):
        state = self.campaign.client.wait_device(self.campaign.device, timeout=timeout,
            grant_seconds=grant_seconds,recovered=True)
        self.campaign.manifest.check_device(state)
        return state

    def backup(self, board, *, address, size, filename, segment_size=4*1024*1024, timeout=600):
        """Sequential read observations, not an atomic snapshot. No automatic retry."""
        if type(size) is not int or size <= 0 or type(segment_size) is not int or not 1 <= segment_size <= 16*1024*1024:
            raise ValueError('Invalid backup size or segment size')
        path = board.output_dir / filename if board.output_dir else None
        if path is None or Path(filename).name != filename or filename in {'.', '..'} or path.suffix == '.json':
            raise ValueError('Backup requires a private output directory and a plain filename')
        if path.exists() or path.with_suffix('.json').exists():
            raise FileExistsError('Backup evidence already exists')
        digest, segments = hashlib.sha256(), []
        partial = path.with_name(path.name + '.partial')
        progress = path.with_name(path.name + '.partial.jsonl')
        with partial.open('x+b') as stream, progress.open('x') as journal:
            _sync_directory(path.parent)
            for offset in range(0,size,segment_size):
                count = min(segment_size,size-offset)
                data = board.read_flash(address=address+offset,size=count,timeout=timeout,return_console=False)
                if len(data) != count:
                    raise BenchError('Backup segment size mismatch')
                stream.write(data)
                _sync_file(stream)
                stream.seek(offset)
                if stream.read(count) != data:
                    raise BenchError('Local backup segment readback mismatch')
                digest.update(data)
                artifact = board.last_operation['result']['artifact']
                segments.append({'address':address+offset,'size':count,'sha256':hashlib.sha256(data).hexdigest(),
                                 'operation':board.last_operation['id']})
                journal.write(json.dumps(segments[-1]) + '\n')
                _sync_file(journal)
                # Durable bytes and their region/hash are retained before server cleanup.
                board.client.delete_artifact(artifact['id'])
        # Publish without replacing any evidence created since preflight.
        os.link(partial, path)
        _sync_directory(path.parent)
        metadata = {'address':address,'size':size,'sha256':digest.hexdigest(),'complete':True,
                    'consistency':'sequential_observations','segments':segments}
        sidecar = path.with_suffix('.json')
        temporary = sidecar.with_name(sidecar.name + '.partial')
        with temporary.open('x') as file:
            file.write(json.dumps(metadata,indent=2))
            _sync_file(file)
        os.link(temporary, sidecar)
        _sync_directory(path.parent)
        for retained in (partial, progress, temporary):
            retained.unlink()
        _sync_directory(path.parent)
        return path, metadata

    def program(self, board, *, address, source, sha256, allow_destructive=False, timeout=600):
        if allow_destructive is not True or board.allow_destructive is not True:
            raise BenchError('Explicit campaign and per-operation destructive consent required')
        # Verify local bytes and manifest sectors before any upload or mutation.
        _duration(timeout, 0.1, 600)
        if isinstance(source,(str,Path)):
            with Path(source).open('rb') as stream:
                data = stream.read(16*1024*1024 + 1)
        else:
            if not isinstance(source,(bytes,bytearray,memoryview)) or len(source) > 16*1024*1024:
                raise ValueError('Expected a binary source up to 16 MiB')
            data = bytes(source)
        if not 0 < len(data) <= 16*1024*1024:
            raise ValueError('Programming source must contain 1..16 MiB')
        if hashlib.sha256(data).hexdigest() != sha256:
            raise BenchError('Programming source SHA-256 mismatch')
        self.campaign.manifest.check_region(board.device_id,address,len(data))
        state = board._check_target()
        budget = 2 * timeout + 90  # write + independent readback, two start windows and margin
        if state['privileged_seconds'] < budget:
            raise BenchError('Local grant is too short for programming and readback')
        if not state.get('reservation') or state['reservation']['remaining_seconds'] < budget:
            raise BenchError('Reservation is too short for programming and readback; renew explicitly')
        artifact = board.client.upload(data)
        result = board.command('flash.write', {'chip':state['chip'],'return_console':False,
            'images':[{'address':address,'artifact':artifact['id'],'sha256':artifact['sha256']}]},
            timeout=timeout,allow_destructive=True)
        readback = board.read_flash(address=address,size=len(data),timeout=timeout,return_console=False)
        if readback != data:
            raise BenchError('Independent programming readback mismatch')
        board.client.delete_artifact(board.last_operation['result']['artifact']['id'])
        board.client.delete_artifact(artifact['id'])
        return result

    def restore(self, board, backup, *, allow_destructive=False, timeout=600):
        if allow_destructive is not True:
            raise BenchError('Explicit restore consent required')
        path = Path(backup)
        metadata = json.loads(path.with_suffix('.json').read_text())
        if not metadata.get('complete'):
            raise BenchError('Backup is incomplete')
        # Validate the entire original before executing the first restoration segment.
        if metadata['address'] % 4096 or metadata['size'] % 4096:
            raise BenchError('Restoration requires a complete set of backed-up erase sectors')
        self.campaign.manifest.check_region(board.device_id,metadata['address'],metadata['size'])
        with path.open('rb') as stream:
            digest, checked_segments, checked_size = hashlib.sha256(), [], 0
            for data in iter(lambda:stream.read(4*1024*1024),b''):
                digest.update(data)
                checked_size += len(data)
                checked_segments.append(hashlib.sha256(data).hexdigest())
            if checked_size != metadata['size'] or digest.hexdigest() != metadata['sha256']:
                raise BenchError('Backup integrity mismatch')
            stream.seek(0)
            offset = 0
            for expected_hash in checked_segments:
                data = stream.read(min(4*1024*1024,metadata['size']-offset))
                if hashlib.sha256(data).hexdigest() != expected_hash:
                    raise BenchError('Backup changed after integrity verification')
                self.program(board,address=metadata['address']+offset,source=data,
                    sha256=expected_hash,allow_destructive=True,timeout=timeout)
                offset += len(data)
        return {'restored':True,'size':metadata['size'],'sha256':metadata['sha256']}

    def compare(self, board, backup, *, timeout=600):
        """Independent sequential comparison against a complete verified backup."""
        path = Path(backup)
        metadata = json.loads(path.with_suffix('.json').read_text())
        reference = hashlib.sha256()
        with path.open('rb') as stream:
            for data in iter(lambda:stream.read(1024*1024),b''):
                reference.update(data)
        if (not metadata.get('complete') or path.stat().st_size != metadata['size'] or
                reference.hexdigest() != metadata['sha256']):
            raise BenchError('Backup integrity mismatch')
        digest = hashlib.sha256()
        for offset in range(0,metadata['size'],4*1024*1024):
            data = board.read_flash(address=metadata['address']+offset,
                size=min(4*1024*1024,metadata['size']-offset),timeout=timeout,return_console=False)
            digest.update(data)
            board.client.delete_artifact(board.last_operation['result']['artifact']['id'])
        if digest.hexdigest() != metadata['sha256']:
            raise BenchError('Final flash comparison differs from the verified backup')
        return {'equal':True,'sha256':digest.hexdigest(),'size':metadata['size'],
                'consistency':'sequential_observations'}

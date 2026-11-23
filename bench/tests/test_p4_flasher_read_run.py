"""Offline checks of the single-shot T1-T5 client: doubles only, no network, no board."""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import p4_flasher_read_run as runner  # noqa: E402
import p4_flasher_read_t1_t5 as t15  # noqa: E402
from test_p4_flasher_read_plan import make_result, reference  # noqa: E402
from server.core import Broker  # noqa: E402
from protocol import P4_FLASHER_READ  # noqa: E402

STUB = json.dumps({'entry': 0x4ff50010, 'text': base64.b64encode(bytes(100)).decode(), 'text_start': 0x4ff50000,
                   'data': base64.b64encode(bytes(40)).decode(), 'data_start': 0x4ff60040, 'bss_start': 0x4ff60000}).encode()
SHA = hashlib.sha256(STUB).hexdigest()
CAPS = ['identify', 'mark', 'diagnostic.p4_usb_trace', 'diagnostic.p4_usb_bringup', P4_FLASHER_READ]


class PendingOperation(Exception):
    pass


class Failure(Exception):
    def __init__(self, operation):
        super().__init__('failed')
        self.operation = operation


class FakeBoard:
    def __init__(self, owner, outcome, generation='GEN', epoch='EPOCH'):
        self.owner, self.outcome, self.calls, self.closed = owner, outcome, [], False
        self.generation, self.epoch, self.uncertain, self.pending = generation, epoch, False, None

    def command(self, name, args, timeout, allow_destructive):
        self.calls.append((name, args, timeout, allow_destructive))
        (self.owner.out / 'operations.jsonl').write_text(json.dumps({'id': 'op1'}) + '\n')
        if self.outcome == 'ok':
            return {'result': make_result()}
        if self.outcome == 'pending':
            raise PendingOperation('unresolved')
        if self.outcome == 'lost_response':      # the POST response was lost: BenchClient marks the board uncertain and raises a network error
            self.uncertain = True
            raise ConnectionError('Transport or response failure; request not retried')
        if self.outcome == 'still_pending':
            self.pending = 'op1'
            raise TimeoutError('poll interrupted')
        raise Failure({'id': 'op1', 'state': 'failed', 'error': {'code': 'diagnostic_flasher_md5_mismatch'}})

    def close(self):
        if self.uncertain:
            raise PendingOperation('Submission outcome unknown; reservation retained until expiry')
        self.closed = True


class FakeClient:
    def __init__(self, outcome='ok', device=None, artifacts=(), reserved_generation='GEN', reserved_epoch='EPOCH'):
        self.outcome, self.board, self.out = outcome, None, None
        self.reserved_generation, self.reserved_epoch = reserved_generation, reserved_epoch
        self.dev = {'id': 'esp32-local', 'generation': 'GEN', 'connected': True, 'capabilities': CAPS, 'quarantined': False,
                    'diagnostic_resident': False, 'reservation': None, 'privileged_seconds': 900,
                    'provenance': {'transport': 'native_usb_other'}, **(device or {})}
        self.stored = list(artifacts)
        self.uploads = 0

    def health(self): return {'epoch': 'EPOCH', 'build': {'git_sha': 'GIT'}}
    def device(self, _): return self.dev
    def artifacts(self): return {'artifacts': self.stored}

    def upload(self, path):
        self.uploads += 1
        return {'id': 'art1', 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(), 'size': len(STUB)}

    def reserve(self, device, ttl, allow_destructive, output_dir):
        assert allow_destructive is True and ttl >= 600
        self.out = Path(output_dir)
        self.board = FakeBoard(self, self.outcome, self.reserved_generation, self.reserved_epoch)
        return self.board

    def operation(self, _): return {'id': 'op1', 'state': 'completed' if self.outcome == 'ok' else 'failed'}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stub = Path(self.tmp.name) / 'stub.json'
        self.stub.write_bytes(STUB)
        self.out = Path(self.tmp.name) / 'run'
        self.expected = {'device': 'esp32-local', 'stub_sha256': SHA, 'plan_sha256': runner.plan_sha256(),
                         'git_sha': 'GIT', 'epoch': 'EPOCH', 'generation': 'GEN'}

    def go(self, client):
        return runner.run(client, self.expected, self.stub, self.out, reference())

    def test_plan_sha_is_the_authorized_one(self):
        self.assertEqual(runner.plan_sha256(), '22e970c7397e5cd7c152d1fe93f4f85e611b4f576673e6ccdbc244cff56d080a')

    def test_the_request_is_accepted_by_the_real_broker_validation_and_queue(self):
        broker = Broker(clock=lambda: 100, p4_flasher_allowlist=[SHA])
        broker.agent('agent', object())
        broker.register('agent', {'id': 'esp32-local', 'chip': 'esp32p4', 'flash_size': None, 'flash_size_known': False,
            'capabilities': ['identify', 'mark', P4_FLASHER_READ],
            'provenance': {'transport': 'native_usb_other', 'transport_basis': 'inferred_from_webserial_vid_pid'}}, ['esp32-local'])
        lease = broker.reserve('esp32-local', 'operator', 600)
        broker.devices['esp32-local']['grant_until'] = 10000
        artifact = broker.artifact(STUB, SHA, 'operator')
        op = broker.submit('operator', ('read', 'control', 'program'), {'lease': lease['id'], 'command': P4_FLASHER_READ,
            'args': runner.build_args('esp32-local', SHA, artifact['id']), 'allow_destructive': True,
            'timeout': runner.COMMAND_TIMEOUT})
        self.assertEqual(op['state'], 'queued')

    def test_success_submits_exactly_once_and_archives_everything(self):
        client = FakeClient()
        self.assertEqual(self.go(client), 'completed')
        self.assertEqual(len(client.board.calls), 1)
        name, args, timeout, allow = client.board.calls[0]
        self.assertEqual((name, timeout, allow), (P4_FLASHER_READ, runner.COMMAND_TIMEOUT, True))
        self.assertEqual(args['plan'], t15.PLAN)
        self.assertTrue(client.board.closed)
        names = {p.name for p in self.out.iterdir()}
        self.assertTrue({'prechecks.json', 'submitted-args.json', 'result.json', 'operation.json', 'post.json', 'evaluation.json', 'SHA256SUMS'} <= names)
        self.assertEqual(json.loads((self.out / 'evaluation.json').read_text())['tests'], {k: 'PASS' for k in ('T1', 'T2', 'T3', 'T4', 'T5')})
        self.assertEqual(oct((self.out).stat().st_mode & 0o777), '0o700')

    def test_failure_is_never_retried_and_keeps_the_operation(self):
        client = FakeClient('failed')
        self.assertEqual(self.go(client), 'failed')
        self.assertEqual(len(client.board.calls), 1)
        self.assertTrue((self.out / 'operation.json').exists())
        self.assertFalse((self.out / 'evaluation.json').exists())

    def test_a_lost_submission_response_stays_indeterminate_and_keeps_the_reservation(self):
        for outcome in ('lost_response', 'still_pending'):
            out = Path(self.tmp.name) / outcome
            client = FakeClient(outcome)
            self.assertEqual(runner.run(client, self.expected, self.stub, out, None), 'indeterminate', outcome)
            self.assertFalse(client.board.closed)
            self.assertEqual(len(client.board.calls), 1)
            self.assertFalse((out / 'evaluation.json').exists())

    def test_a_target_that_changed_between_prechecks_and_reservation_is_never_commanded(self):
        for kwargs in ({'reserved_generation': 'NEW'}, {'reserved_epoch': 'NEWEPOCH'}):
            out = Path(self.tmp.name) / ('chg' + next(iter(kwargs)))
            client = FakeClient(**kwargs)
            with self.assertRaises(runner.Refusal) as caught:
                runner.run(client, self.expected, self.stub, out, None)
            self.assertIn('nothing submitted', str(caught.exception))
            self.assertEqual(client.board.calls, [])          # board.command() was never reached
            self.assertTrue(client.board.closed)              # the reservation was released
            self.assertTrue((out / 'refused-after-reservation.json').exists())

    def test_pending_outcome_keeps_the_reservation(self):
        client = FakeClient('pending')
        self.assertEqual(self.go(client), 'indeterminate')
        self.assertFalse(client.board.closed)
        self.assertEqual(len(client.board.calls), 1)

    def test_refusals_happen_before_any_upload_or_reservation(self):
        for device in ({'generation': 'OTHER'}, {'connected': False}, {'quarantined': True}, {'diagnostic_resident': True},
                       {'reservation': {'id': 'x'}}, {'privileged_seconds': 100}, {'capabilities': CAPS[:-1]},
                       {'capabilities': CAPS + ['flash.write']}, {'provenance': {'transport': 'uart'}}):
            client = FakeClient(device=device)
            out = Path(self.tmp.name) / ('r' + str(abs(hash(json.dumps(device, sort_keys=True)))))
            with self.assertRaises(runner.Refusal, msg=str(device)):
                runner.run(client, self.expected, self.stub, out, None)
            self.assertEqual(client.uploads, 0)
            self.assertIsNone(client.board)

    def test_wrong_candidate_wrong_plan_stale_epoch_or_artifacts_are_refused(self):
        with self.assertRaises(runner.Refusal):
            runner.run(FakeClient(), {**self.expected, 'stub_sha256': '0' * 64}, self.stub, self.out, None)
        with self.assertRaises(runner.Refusal):
            runner.run(FakeClient(), {**self.expected, 'plan_sha256': '0' * 64}, self.stub, Path(self.tmp.name) / 'p', None)
        with self.assertRaises(runner.Refusal):
            runner.run(FakeClient(), {**self.expected, 'epoch': 'OLD'}, self.stub, Path(self.tmp.name) / 'e', None)
        with self.assertRaises(runner.Refusal):
            runner.run(FakeClient(artifacts=[{'id': 'old'}]), self.expected, self.stub, Path(self.tmp.name) / 'a', None)

    def test_an_existing_evidence_directory_is_refused(self):
        self.out.mkdir()
        with self.assertRaises(FileExistsError):
            self.go(FakeClient())

    def test_cli_is_a_dry_run_and_needs_the_operator_string_to_execute(self):
        base = ['--stub', str(self.stub), '--stub-sha256', SHA, '--git-sha', 'GIT', '--epoch', 'EPOCH', '--generation', 'GEN', '--out', str(self.out)]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('IDLAB_FLASHER_AUTHORIZED', None)
            self.assertEqual(runner.main(base), 0)                      # nothing submitted
            self.assertFalse(self.out.exists())
            self.assertEqual(runner.main(base + ['--execute']), 2)      # no authorization string
        with mock.patch.dict(os.environ, {'IDLAB_FLASHER_AUTHORIZED': 'f' * 64}):
            self.assertEqual(runner.main(base + ['--execute']), 2)      # wrong string
        self.assertFalse(self.out.exists())

    def test_authorized_execute_wires_the_client_and_returns_the_verdict(self):
        tokens = Path(self.tmp.name) / 'tokens.json'
        tokens.write_text(json.dumps({'secret-a': {'id': 'other', 'role': 'agent'}, 'secret-b': {'id': 'claude', 'role': 'client'}}))
        seen = {}
        client = FakeClient()

        def factory(url, token):
            seen['args'] = (url, token)
            return client
        import iobewi_bench
        base = ['--stub', str(self.stub), '--stub-sha256', SHA, '--git-sha', 'GIT', '--epoch', 'EPOCH', '--generation', 'GEN',
                '--out', str(self.out), '--tokens', str(tokens), '--url', 'http://127.0.0.1:1', '--execute']
        with mock.patch.dict(os.environ, {'IDLAB_FLASHER_AUTHORIZED': runner.plan_sha256()}), mock.patch.object(iobewi_bench, 'BenchClient', factory):
            self.assertEqual(runner.main(base), 0)
        self.assertEqual(seen['args'], ('http://127.0.0.1:1', 'secret-b'))
        self.assertEqual(len(client.board.calls), 1)


if __name__ == '__main__':
    unittest.main()

"""A stub-reported-running refusal keeps its admission observations from the agent down to the broker's operation.

Durable: the stored operation is also re-read from SQLite by a second Broker on the same state directory.
Software only: the real Agent + EspEngine + runP4UsbBringup run in Node against a simulated P4 (see
p4-admission-agent-message.mjs); the message the agent sends is then fed to the real Broker. No board, browser or network."""
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.core import Broker
from protocol import P4_BRINGUP

HELPER = Path(__file__).with_name('p4-admission-agent-message.mjs')


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for the agent side')
class AdmissionEvidencePersistenceTests(unittest.TestCase):
    def run_scenario(self, state_dir):
        data = json.dumps({'entry': 0x4ff50010, 'text': base64.b64encode(bytes(100)).decode(), 'text_start': 0x4ff50000,
                           'data': base64.b64encode(bytes(40)).decode(), 'data_start': 0x4ff60040,
                           'bss_start': 0x4ff60000}).encode()
        sha = hashlib.sha256(data).hexdigest()
        b = Broker(clock=lambda: 100, state_dir=state_dir, p4_bringup_allowlist=[sha])
        b.agent('agent', object())
        b.register('agent', {'id': 'p4', 'chip': 'esp32p4', 'flash_size': 32 * 1024 * 1024,
                             'capabilities': ['identify', 'mark', P4_BRINGUP],
                             'provenance': {'transport': 'native_usb_other', 'transport_basis': 'inferred_from_webserial_vid_pid'}}, ['p4'])
        lease = b.reserve('p4', 'operator', 600)
        b.devices['p4']['grant_until'] = 10000
        artifact = b.artifact(data, sha, 'operator')
        args = {'chip': 'esp32p4', 'stub_artifact': artifact['id'], 'stub_sha256': sha, 'scenario': 'ohai_ping2',
                'consent': {'scenario': P4_BRINGUP, 'device': 'p4', 'stub_sha256': sha,
                            'ram_code_execution': True, 'rom_reset_may_be_required': True}}
        op = b.submit('operator', ('read', 'control', 'program'), {'lease': lease['id'], 'command': P4_BRINGUP,
                                                                    'args': args, 'allow_destructive': True})
        b.offer('p4')
        b.commit('agent', op['id'])
        self.assertTrue(b.devices['p4']['diagnostic_resident'])

        run = subprocess.run(['node', str(HELPER)], input=json.dumps({'op_id': op['id'], 'device': {'id': 'p4', 'chip': 'esp32p4'},
                             'args': args, 'stub_b64': base64.b64encode(data).decode()}),
                             capture_output=True, text=True, timeout=60, check=True)
        produced = json.loads(run.stdout)
        message, calls = produced['message'], produced['calls']

        self.assertEqual(message['state'], 'failed')
        self.assertEqual(message['error']['code'], 'diagnostic_stub_reported_running')
        self.assertFalse([c for c in calls if c.startswith(('MEM_', 'FLASH')) or c in {'WRITE', 'READ'}], calls)
        self.assertEqual(calls.count('sync'), 1)   # no retry of the admission

        b.finish('agent', {key: value for key, value in message.items() if key != 'type'})
        stored = b.operations[op['id']]
        self.assertEqual(stored['state'], 'failed')
        error = stored['error']
        self.assertEqual(error['code'], 'diagnostic_stub_reported_running')
        self.assertEqual(error['sync_values'], [0])
        self.assertEqual(error['admission_verdict'], 'stub')
        self.assertEqual(error['ram_load'], 'not_started')
        # The current conservative policy is untouched: no automatic lift of the quarantine for this verdict.
        self.assertTrue(b.devices['p4']['quarantined'])
        self.assertTrue(b.devices['p4']['diagnostic_resident'])

        # Settle and close the journal: the caller reopens a NEW broker on the same state directory (SQLite, not memory).
        b.agent_event('agent', {'type': 'settled', 'device': 'p4', 'generation': b.devices['p4']['generation'], 'id': op['id']})
        b.journal.close()
        return op['id'], sha

    def reopened(self, state_dir, sha):
        return Broker(clock=lambda: 100, state_dir=state_dir, p4_bringup_allowlist=[sha])

    def test_stub_refusal_keeps_observations_flags_and_quarantine(self):
        with tempfile.TemporaryDirectory() as state_dir:
            op_id, sha = self.run_scenario(state_dir)
            again = self.reopened(state_dir, sha)
            try:
                kept = again.operations[op_id]
                self.assertEqual(kept['state'], 'failed')
                self.assertEqual(kept['error']['code'], 'diagnostic_stub_reported_running')
                self.assertEqual(kept['error']['phase'], 'diagnostic_stub_load')
                self.assertTrue(again.devices['p4']['quarantined'])
                self.assertTrue(again.devices['p4']['diagnostic_resident'])
            finally:
                again.journal.close()

    def test_observations_survive_a_server_restart(self):
        """The journal (PR #81) keeps sync_values / admission_verdict / ram_load of a P4 diagnostic error across a restart."""
        with tempfile.TemporaryDirectory() as state_dir:
            op_id, sha = self.run_scenario(state_dir)
            again = self.reopened(state_dir, sha)
            try:
                error = again.operations[op_id]['error']
                self.assertEqual(error['sync_values'], [0])
                self.assertEqual(error['admission_verdict'], 'stub')
                self.assertEqual(error['ram_load'], 'not_started')
            finally:
                again.journal.close()


if __name__ == '__main__':
    unittest.main()

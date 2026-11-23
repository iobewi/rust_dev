"""Read-only flasher stub command: broker contract and JS/Python consent parity; no board or browser access."""
import base64
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.core import Broker
from protocol import P4_FLASHER_READ, Fault, p4_flasher_consent

PLAN = [{'op': 'attach'}, {'op': 'read', 'address': 0x00fff000, 'size': 8192, 'packet_size': 4096},
        {'op': 'md5', 'address': 0, 'size': 4096}]


def stub_bytes():
    return json.dumps({'entry': 0x4ff50010, 'text': base64.b64encode(bytes(100)).decode(), 'text_start': 0x4ff50000,
                       'data': base64.b64encode(bytes(40)).decode(), 'data_start': 0x4ff60040,
                       'bss_start': 0x4ff60000}).encode()


class FlasherReadBrokerTests(unittest.TestCase):
    def setUp(self):
        self.data = stub_bytes()
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.b = Broker(clock=lambda: 100, p4_flasher_allowlist=[self.sha])
        self.b.agent('agent', object())
        self.b.register('agent', {'id': 'p4', 'chip': 'esp32p4', 'flash_size': None, 'flash_size_known': False,
            'capabilities': ['identify', 'mark', P4_FLASHER_READ],
            'provenance': {'transport': 'native_usb_other', 'transport_basis': 'inferred_from_webserial_vid_pid'}}, ['p4'])
        self.lease = self.b.reserve('p4', 'operator', 600)
        self.b.devices['p4']['grant_until'] = 10000
        self.artifact = self.b.artifact(self.data, self.sha, 'operator')

    def args(self, plan=None):
        plan = PLAN if plan is None else plan
        return {'chip': 'esp32p4', 'stub_artifact': self.artifact['id'], 'stub_sha256': self.sha,
                'scenario': 'read_only_v1', 'plan': plan, 'consent': p4_flasher_consent('p4', self.sha, plan)}

    def submit(self, args=None, scopes=('read', 'control', 'program'), allow=True):
        return self.b.submit('operator', scopes, {'lease': self.lease['id'], 'command': P4_FLASHER_READ,
            'args': self.args() if args is None else args, 'allow_destructive': allow})

    def refused(self, code, **kwargs):
        with self.assertRaises(Fault) as err:
            self.submit(**kwargs)
        self.assertEqual(err.exception.code, code)

    def test_accepts_the_exact_plan_and_binds_the_artifact(self):
        op = self.submit()
        self.assertEqual(op['state'], 'queued')
        self.assertIn(self.artifact['id'], self.b.operations[op['id']]['artifact_ids'])

    def test_no_write_erase_register_or_parameter_step_can_be_submitted(self):
        for step in ({'op': 'write', 'address': 0, 'size': 1}, {'op': 'erase', 'address': 0, 'size': 4096},
                     {'op': 'write_reg', 'address': 0x5008d018, 'size': 4}, {'op': 'set_params'},
                     {'op': 'read', 'address': 0, 'size': 1, 'packet_size': 4096, 'flash_begin': True}):
            self.refused('diagnostic_parameter_not_allowed', args=self.args([step]))

    def test_plan_bounds_and_attach_position(self):
        for plan in ([], [{'op': 'md5', 'address': 0, 'size': 1}, {'op': 'attach'}],
                     [{'op': 'read', 'address': (1 << 25) - 1, 'size': 2, 'packet_size': 4096}],
                     [{'op': 'read', 'address': 0, 'size': 4096, 'packet_size': 512}],
                     [{'op': 'read', 'address': 0, 'size': 4 * 1024 * 1024 + 1, 'packet_size': 4096}],
                     [{'op': 'read', 'address': 0, 'size': 4 * 1024 * 1024, 'packet_size': 4096}] * 5,
                     [{'op': 'md5', 'address': True, 'size': 1}]):
            self.refused('diagnostic_parameter_not_allowed', args=self.args(plan))

    def test_consent_is_bound_to_board_stub_and_plan(self):
        a = self.args(); a['consent'] = p4_flasher_consent('p4', self.sha, [{'op': 'md5', 'address': 0, 'size': 8192}])
        self.refused('diagnostic_consent_invalid', args=a)
        a = self.args(); a['consent'] = {**a['consent'], 'flash_write_forbidden': False}
        self.refused('diagnostic_consent_invalid', args=a)
        a = self.args(); a['scenario'] = 'ohai_ping2'
        self.refused('diagnostic_parameter_not_allowed', args=a)

    def test_allowlist_scopes_and_programming_consent(self):
        self.b.p4_flasher_allowlist = frozenset()
        self.refused('diagnostic_stub_not_qualified')
        self.b.p4_flasher_allowlist = frozenset([self.sha])
        self.refused('program_permission_required', scopes=('read', 'control'))
        self.refused('destructive_consent_required', allow=False)
        self.b.devices['p4']['grant_until'] = 0
        self.refused('grant_required')

    def test_bringup_allowlist_does_not_authorize_the_flasher(self):
        self.b.p4_flasher_allowlist = frozenset()
        self.b.p4_bringup_allowlist = frozenset([self.sha])
        self.refused('diagnostic_stub_not_qualified')

    def test_exclusive_board_and_quarantine_after_start(self):
        op = self.submit()
        self.refused('diagnostic_in_progress')
        self.b.offer('p4')
        self.b.commit('agent', op['id'])
        self.assertTrue(self.b.devices['p4']['diagnostic_resident'])
        self.b.finish('agent', {'id': op['id'], 'state': 'completed', 'result': {'steps': []}, 'hold': True})
        self.assertTrue(self.b.devices['p4']['quarantined'])
        self.assertTrue(self.b.devices['p4']['diagnostic_resident'])

    def test_javascript_and_python_derive_the_same_consent(self):
        script = ("import {expectedFlasherConsent} from './web/p4-flasher-read.js';"
                  f"const plan = {json.dumps(PLAN)};"
                  f"console.log(JSON.stringify(await expectedFlasherConsent('p4', '{self.sha}', plan)));")
        out = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout), p4_flasher_consent('p4', self.sha, PLAN))


if __name__ == '__main__':
    unittest.main()

"""RAM-only P4 bring-up broker contract; no board or browser access."""
import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.core import Broker
from protocol import P4_BRINGUP, Fault


def stub_bytes():
    return json.dumps({'entry': 0x4ff50010,
        'text': base64.b64encode(bytes(100)).decode(), 'text_start': 0x4ff50000,
        'data': base64.b64encode(bytes(40)).decode(), 'data_start': 0x4ff60040,
        'bss_start': 0x4ff60000}).encode()


class P4BringupBrokerTests(unittest.TestCase):
    def setUp(self):
        self.data = stub_bytes()
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.b = Broker(clock=lambda: 100, p4_bringup_allowlist=[self.sha])
        self.b.agent('agent', object())
        self.b.register('agent', {'id': 'p4', 'chip': 'esp32p4', 'flash_size': 32 * 1024 * 1024,
            'capabilities': ['identify', 'mark', P4_BRINGUP],
            'provenance': {'transport': 'native_usb_other',
                'transport_basis': 'inferred_from_webserial_vid_pid'}}, ['p4'])
        self.lease = self.b.reserve('p4', 'operator', 600)
        self.b.devices['p4']['grant_until'] = 10000
        self.artifact = self.b.artifact(self.data, self.sha, 'operator')

    def args(self):
        return {'chip': 'esp32p4', 'stub_artifact': self.artifact['id'], 'stub_sha256': self.sha,
            'scenario': 'ohai_ping2', 'consent': {'scenario': P4_BRINGUP, 'device': 'p4',
                'stub_sha256': self.sha, 'ram_code_execution': True,
                'rom_reset_may_be_required': True}}

    def submit(self, args=None, scopes=('read', 'control', 'program'), allow=True):
        return self.b.submit('operator', scopes, {'lease': self.lease['id'],
            'command': P4_BRINGUP, 'args': self.args() if args is None else args,
            'allow_destructive': allow})

    def refused(self, code, **kwargs):
        with self.assertRaises(Fault) as err:
            self.submit(**kwargs)
        self.assertEqual(err.exception.code, code)

    def test_consent_sha_allowlist_and_schema(self):
        a = self.args(); a['consent'] = {**a['consent'], 'device': 'other'}
        self.refused('diagnostic_consent_invalid', args=a)
        a = self.args(); a['scenario'] = 'read_flash'
        self.refused('diagnostic_parameter_not_allowed', args=a)
        a = self.args(); a['offset'] = 0x8000
        self.refused('invalid_request', args=a)
        op = self.submit()
        self.assertEqual(op['state'], 'queued')
        self.assertIn(self.artifact['id'], self.b.operations[op['id']]['artifact_ids'])

    def test_qualified_hash_and_program_consent_required(self):
        self.b.p4_bringup_allowlist = frozenset()
        self.refused('diagnostic_stub_not_qualified')
        self.b.p4_bringup_allowlist = frozenset([self.sha])
        self.refused('program_permission_required', scopes=('read', 'control'))
        self.refused('destructive_consent_required', allow=False)

    def test_new_diagnostic_reserves_exclusive_board_and_stays_quarantined(self):
        op = self.submit()
        self.b.offer('p4')
        self.b.commit('agent', op['id'])
        self.assertTrue(self.b.devices['p4']['diagnostic_resident'])
        self.b.finish('agent', {'id': op['id'], 'state': 'completed',
            'result': {'greeting': 'OHAI', 'exchanges': [1, 2]},
            'hold': True})
        self.assertTrue(self.b.devices['p4']['quarantined'])
        self.assertTrue(self.b.devices['p4']['diagnostic_resident'])


if __name__ == '__main__':
    unittest.main()

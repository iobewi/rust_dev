"""P4 admission evidence: bounded durable fields survive SQLite restart, nothing sensitive does."""
import hashlib
import json
import base64
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.core import Broker
from server.journal import safe_operation_error
from protocol import P4_BRINGUP


class AdmissionJournalTests(unittest.TestCase):
    def test_strict_allowlist(self):
        raw = {'code': 'diagnostic_stub_reported_running', 'phase': 'diagnostic_stub_load',
               'sync_values': [0, -2147483648, 2147483647], 'admission_verdict': 'stub',
               'ram_load': 'not_started', 'raw_packet': 'SECRET', 'stack': 'PRIVATE',
               'md5_shape': 'stub', 'arbitrary': {'token': 'PRIVATE'}}
        safe = safe_operation_error(raw, P4_BRINGUP)
        self.assertEqual(safe['sync_values'], [0, -2147483648, 2147483647])
        self.assertEqual(safe['admission_verdict'], 'stub')
        self.assertEqual(safe['ram_load'], 'not_started')
        self.assertEqual(set(safe), {'code', 'phase', 'sync_values', 'admission_verdict', 'ram_load'})
        # No other command may persist admission observations.
        self.assertEqual(safe_operation_error(raw, 'flash.write'),
                         {'code': raw['code'], 'phase': raw['phase']})

    def test_malformed_values_are_dropped_not_coerced(self):
        invalid_sync = [[0] * 9, [0, '1'], [True], [2147483648], [-2147483649],
                        {'0': 0}, '0', [1.2], [None]]
        for values in invalid_sync:
            with self.subTest(values=values):
                safe = safe_operation_error({'code': 'diagnostic_bad', 'sync_values': values}, P4_BRINGUP)
                self.assertNotIn('sync_values', safe)
        for value in ['invalid', '', None, 4, True, ['stub']]:
            with self.subTest(value=value):
                safe = safe_operation_error({'code': 'diagnostic_bad', 'admission_verdict': value,
                                             'ram_load': value}, P4_BRINGUP)
                self.assertNotIn('admission_verdict', safe)
                self.assertNotIn('ram_load', safe)
        self.assertEqual(safe_operation_error(None, P4_BRINGUP), None)

    def test_sqlite_reopen_preserves_only_qualified_admission_fields(self):
        data = json.dumps({'entry': 0x4ff50010, 'text': base64.b64encode(bytes(100)).decode(),
                           'text_start': 0x4ff50000, 'data': base64.b64encode(bytes(40)).decode(),
                           'data_start': 0x4ff60040, 'bss_start': 0x4ff60000}).encode()
        sha = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            b = Broker(clock=lambda: 100, state_dir=directory, p4_bringup_allowlist=[sha])
            b.agent('agent', object())
            b.register('agent', {'id': 'p4', 'chip': 'esp32p4', 'flash_size': 32 * 1024 * 1024,
                'capabilities': ['identify', 'mark', P4_BRINGUP],
                'provenance': {'transport': 'native_usb_other',
                               'transport_basis': 'inferred_from_webserial_vid_pid'}}, ['p4'])
            lease = b.reserve('p4', 'operator', 600)
            b.devices['p4']['grant_until'] = 10000
            artifact = b.artifact(data, sha, 'operator')
            args = {'chip': 'esp32p4', 'stub_artifact': artifact['id'], 'stub_sha256': sha,
                    'scenario': 'ohai_ping2',
                    'consent': {'scenario': P4_BRINGUP, 'device': 'p4', 'stub_sha256': sha,
                                'ram_code_execution': True, 'rom_reset_may_be_required': True}}
            op = b.submit('operator', ('read', 'control', 'program'), {'lease': lease['id'],
                'command': P4_BRINGUP, 'args': args, 'allow_destructive': True})
            b.offer('p4')
            b.commit('agent', op['id'])
            b.finish('agent', {'id': op['id'], 'state': 'failed',
                'error': {'code': 'diagnostic_stub_reported_running', 'phase': 'diagnostic_stub_load',
                          'category': 'refused_before_load', 'ram_load': 'not_started',
                          'sync_values': [0], 'admission_verdict': 'stub',
                          'raw_packet': 'SECRET', 'md5_shape': 'stub'}, 'hold': False})
            b.agent_event('agent', {'type': 'settled', 'device': 'p4',
                                    'generation': b.devices['p4']['generation'], 'id': op['id']})
            self.assertTrue(b.devices['p4']['quarantined'])
            self.assertTrue(b.devices['p4']['diagnostic_resident'])
            b.journal.close()
            again = Broker(clock=lambda: 100, state_dir=directory, p4_bringup_allowlist=[sha])
            try:
                stored = again.operations[op['id']]
                self.assertEqual(stored['state'], 'failed')
                self.assertEqual(stored['error']['sync_values'], [0])
                self.assertEqual(stored['error']['admission_verdict'], 'stub')
                self.assertEqual(stored['error']['ram_load'], 'not_started')
                self.assertNotIn('raw_packet', stored['error'])
                self.assertNotIn('md5_shape', stored['error'])
                self.assertTrue(again.devices['p4']['quarantined'])
                self.assertTrue(again.devices['p4']['diagnostic_resident'])
            finally:
                again.journal.close()


if __name__ == '__main__':
    unittest.main()

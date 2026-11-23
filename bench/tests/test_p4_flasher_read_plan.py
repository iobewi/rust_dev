"""Offline verdict rules of the T1-T5 flasher read plan, against a synthetic NOR (no hardware, no real dump)."""
import copy
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import p4_flasher_read_t1_t5 as t15  # noqa: E402

NOR = bytearray(b'\xff' * (1 << 25))
for start in (0x2000, 0x100000, 0x200000):   # non-blank areas, like a bootloader and data; blank around 16 MiB
    size = 0x4000 if start == 0x2000 else 0x300000 // 3
    NOR[start:start + size] = bytes((i * 131 + (i >> 7) + start) & 0xFE for i in range(size))   # never 0xFF


def capture(address, size, wrap=False):
    if wrap and address >= 0x1000000:
        address &= 0xFFFFFF
    return bytes(NOR[address:address + size])


def make_result(fault=None):
    steps = []
    for index, plan in enumerate(t15.PLAN):
        data = capture(plan['address'], plan['size'], wrap=fault == 'wrap')
        md5 = hashlib.md5(data).hexdigest()
        if plan['op'] == 'md5':
            steps.append({'op': 'md5', 'address': plan['address'], 'size': plan['size'], 'md5_stub': md5})
        else:
            steps.append({'op': 'read', 'address': plan['address'], 'size': plan['size'], 'packet_size': plan['packet_size'],
                          'bytes': len(data), 'packets': -(-plan['size'] // plan['packet_size']), 'duration_ms': 1000,
                          'throughput_kib_s': 1024.0 if plan['size'] == 1 << 20 else 50.0,
                          'data_sha256': hashlib.sha256(data).hexdigest(), 'md5_host': md5, 'md5_stub': md5, 'md5_match': True})
    result = {'steps': steps, 'opcodes_sent': [0x13, 0xD2], 'nor_written': False}
    if fault == 'md5_mismatch':
        steps[0]['md5_match'] = False
    if fault == 'repeat_differs':
        steps[21]['data_sha256'] = '0' * 64
    if fault == 'independent_md5':
        steps[20]['md5_stub'] = '0' * 32
    if fault == 'opcode':
        result['opcodes_sent'] = [0x02, 0x13, 0xD2]
    return result


def reference():
    rows = []
    for index, plan in enumerate(t15.PLAN):
        if plan['op'] == 'read':
            rows.append({'i': index, 'ref_sha256': hashlib.sha256(capture(plan['address'], plan['size'])).hexdigest()})
    return rows


class PlanTests(unittest.TestCase):
    def test_plan_is_within_the_command_limits_and_labelled(self):
        self.assertEqual(len(t15.PLAN), len(t15.TESTS))
        self.assertLessEqual(sum(s['size'] for s in t15.PLAN if s['op'] == 'read'), 16 * 1024 * 1024)
        self.assertEqual(sorted(set(t15.TESTS)), ['T1', 'T2', 'T3', 'T4', 'T5'])
        self.assertTrue(any(s['address'] < 0x1000000 < s['address'] + s['size'] for s in t15.PLAN))   # crossings exist
        self.assertFalse(any(s['op'] not in {'read', 'md5'} for s in t15.PLAN))

    def test_all_pass_with_reference(self):
        out = t15.evaluate(make_result(), reference())
        self.assertEqual(out['tests'], {k: 'PASS' for k in ('T1', 'T2', 'T3', 'T4', 'T5')})
        self.assertAlmostEqual(out['throughput']['mean_kib_s'], 1024.0)

    def test_boundary_needs_a_reference(self):
        self.assertEqual(t15.evaluate(make_result())['tests']['T3'], 'INDETERMINATE')

    def test_wrap_at_16_mib_is_not_a_pass(self):
        out = t15.evaluate(make_result('wrap'), reference())
        self.assertNotEqual(out['tests']['T3'], 'PASS')
        self.assertEqual(out['tests']['T1'], 'PASS')

    def test_faults(self):
        self.assertEqual(t15.evaluate(make_result('md5_mismatch'), reference())['tests']['T1'], 'FAIL')
        self.assertEqual(t15.evaluate(make_result('repeat_differs'), reference())['tests']['T5'], 'FAIL')
        self.assertEqual(t15.evaluate(make_result('independent_md5'), reference())['tests']['T4'], 'FAIL')
        self.assertEqual(set(t15.evaluate(make_result('opcode'), reference())['tests'].values()), {'FAIL'})

    def test_incomplete_result_is_indeterminate(self):
        self.assertEqual(set(t15.evaluate({'steps': []})['tests'].values()), {'INDETERMINATE'})

    def test_changed_card_is_reported_not_failed(self):
        changed = copy.deepcopy(reference())
        changed[11]['ref_sha256'] = '1' * 64
        self.assertEqual(t15.evaluate(make_result(), changed)['tests']['T3'], 'INDETERMINATE')


if __name__ == '__main__':
    unittest.main()

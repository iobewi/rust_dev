import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('cmp', Path(__file__).resolve().parents[1] / 'diagnostics/p4_usb_trace/compare_packet_hashes.py')
cmp = importlib.util.module_from_spec(spec); spec.loader.exec_module(cmp)

BASE, PKT = 0x8000, 4096


def flash(n=64):
    """pseudo-flash: erased packets (0xFF) and packets with content"""
    out = bytearray()
    for i in range(n):
        out += bytes([0xFF]) * PKT if i % 4 else bytes((j * 7 + i) & 0xFE for j in range(PKT))
    return bytes(out)


def result_for(data, base=BASE, mutate=None):
    hashes = []
    for i in range(len(data) // PKT):
        chunk = data[i * PKT:(i + 1) * PKT]
        hashes.append({'packet': i, 'address': base + i * PKT, 'size': PKT, 'sha256': hashlib.sha256(chunk).hexdigest(), 'ack_index': i + 1, 'ack_bytes': (i + 1) * PKT, 'acked': 1})
    r = {'address': base, 'size': len(data), 'data_sha256': hashlib.sha256(data).hexdigest(), 'packet_hashes': hashes}
    if mutate:
        mutate(r)
    return r


class ComparePacketHashes(unittest.TestCase):
    def test_identical_run_matches_everywhere(self):
        d = flash(); s = cmp.compare(result_for(d), d)
        self.assertEqual((s['packets'], s['identical'], s['different'], s['runs'], s['periodic']), (64, 64, 0, [], None))
        self.assertTrue(s['global']['global_match'])

    def test_localises_the_differing_packets_and_splits_by_content(self):
        ref = flash(); t = bytearray(ref)
        for p in (0, 5, 9):                                   # packet 0 has content (i % 4 == 0), 5 and 9 are erased packets in the reference
            t[p * PKT + 17] ^= 0x55
        s = cmp.compare(result_for(bytes(t)), ref)
        self.assertEqual((s['identical'], s['different'], s['different_packets']), (61, 3, [0, 5, 9]))
        self.assertEqual(s['by_content_of_reference'], {'erased': {'identical': 46, 'different': 2}, 'with_data': {'identical': 15, 'different': 1}})
        self.assertFalse(s['global']['global_match'])
        self.assertEqual([r['address_from'] for r in s['runs']], [BASE, BASE + 5 * PKT, BASE + 9 * PKT])

    def test_runs_and_periodic_pattern(self):
        ref = flash(64); t = bytearray(ref)
        for p in range(3, 64, 8):                              # every 8th packet
            t[p * PKT] ^= 1
        s = cmp.compare(result_for(bytes(t)), ref)
        self.assertEqual(s['different'], 8)
        self.assertEqual(s['periodic'], {'most_common_delta': 8, 'common_stride': 8, 'all_on_stride': True, 'first_packet': 3})
        t2 = bytearray(ref)
        for p in range(10, 20):
            t2[p * PKT] ^= 1
        s2 = cmp.compare(result_for(bytes(t2)), ref)
        self.assertEqual(s2['runs'], [{'from': 10, 'to': 19, 'count': 10, 'address_from': BASE + 10 * PKT}])
        self.assertFalse(s2['periodic']['all_on_stride'])

    def test_no_flash_byte_is_ever_printed(self):
        ref = flash(); t = bytearray(ref); t[3] ^= 0xFF
        text = json.dumps(cmp.compare(result_for(bytes(t)), ref))
        self.assertNotIn(ref[:32].hex(), text); self.assertEqual(set(cmp.compare(result_for(bytes(t)), ref)['global']) , {'bytes', 'reference_sha256', 't_data_sha256', 'global_match'})

    def test_unusable_inputs_are_refused(self):
        d = flash()
        for mut, msg in ((lambda r: r.pop('packet_hashes'), 'no_packet_hashes'), (lambda r: r['packet_hashes'].pop(3), 'invalid_packet_layout_at_3'),
                         (lambda r: r['packet_hashes'][2].update(address=1), 'invalid_packet_layout_at_2'), (lambda r: r['packet_hashes'][1].update(size=0), 'invalid_packet_layout_at_1')):
            with self.assertRaises(ValueError) as c:
                cmp.compare(result_for(d, mutate=mut), d)
            self.assertIn(msg, str(c.exception))
        with self.assertRaises(ValueError):
            cmp.compare(result_for(d), d[:-PKT])             # dump of the wrong size

    def test_cli_exit_codes_and_phase_results_format(self):
        ref = flash(); t = bytearray(ref); t[PKT * 2 + 1] ^= 1
        with tempfile.TemporaryDirectory() as tmp:
            same, diff, dump = (os.path.join(tmp, n) for n in ('same.json', 'diff.json', 'ref.bin'))
            open(dump, 'wb').write(ref)
            json.dump({'status': 'completed', 'result': result_for(ref)}, open(same, 'w'))     # phaseN-results.json shape
            json.dump(result_for(bytes(t)), open(diff, 'w'))                                    # bare result shape
            self.assertEqual(cmp.main([same, dump]), 0)
            self.assertEqual(cmp.main([diff, dump]), 1)
            self.assertEqual(cmp.main([diff, os.path.join(tmp, 'missing.bin')]), 2)
            self.assertEqual(cmp.main([diff]), 2)


if __name__ == '__main__':
    unittest.main()

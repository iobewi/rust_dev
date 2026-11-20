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

    # ---- invalid evidence is never a success -------------------------------------------------------------------------------------------
    def invalid(self, result, dump, *needles):
        with self.assertRaises(cmp.InvalidEvidence) as c:
            cmp.compare(result, dump)
        text = " | ".join(c.exception.problems)
        for n in needles:
            self.assertIn(n, text)
        return text

    def test_256_matching_packets_with_a_contradictory_global_sha256_are_invalid_not_a_success(self):
        d = flash(256)
        r = result_for(d)
        r['data_sha256'] = hashlib.sha256(b'another dump').hexdigest()         # well-formed but not the digest of these packets
        self.invalid(r, d, 'per_packet_hashes_contradict_global_sha256', 'all packets match the reference')
        self.assertEqual(cmp.compare(result_for(d), d)['status'], 'match')       # the consistent twin is a real match
        self.assertEqual(len(result_for(d)['packet_hashes']), 256)

    def test_the_reverse_contradiction_is_invalid_too(self):
        ref = flash(); t = bytearray(ref); t[PKT * 3] ^= 1
        r = result_for(bytes(t)); r['data_sha256'] = hashlib.sha256(ref).hexdigest()      # global says "same as reference", a packet says otherwise
        self.invalid(r, ref, 'per_packet_hashes_contradict_global_sha256', 'some packet hashes differ')

    def test_inconsistent_ack_evidence_is_invalid(self):
        d = flash()
        for mut, needle in ((lambda r: r['packet_hashes'][5].update(ack_bytes=1), 'packet_5_ack_bytes_expected_24576'),
                            (lambda r: r['packet_hashes'][5].update(ack_index=9), 'packet_5_ack_index_expected_6'),
                            (lambda r: r['packet_hashes'][5].update(acked=0), 'packet_5_not_acked'),
                            (lambda r: r['packet_hashes'][5].update(acked=True), 'packet_5_not_acked'),
                            (lambda r: r['packet_hashes'][0].pop('ack_bytes'), 'packet_0_ack_bytes_expected_4096'),
                            (lambda r: r.update(acks=63), 'declared_acks_63_differs_from_packet_count_64')):
            self.invalid(result_for(d, mutate=mut), d, needle)

    def test_wrong_ranges_are_invalid(self):
        d = flash(); total = len(d)
        self.invalid(result_for(d, mutate=lambda r: r['packet_hashes'].pop(3)), d, 'packet_number_3')                  # dropped packet
        self.invalid(result_for(d, mutate=lambda r: r['packet_hashes'][2].update(address=1)), d, 'packet_2_address_expected')
        self.invalid(result_for(d, mutate=lambda r: r['packet_hashes'][2].update(address=BASE + 3 * PKT)), d, 'packet_2_address_expected')   # duplicated address
        self.invalid(result_for(d, mutate=lambda r: r['packet_hashes'][1].update(size=0)), d, 'packet_1_size')
        self.invalid(result_for(d, mutate=lambda r: r['packet_hashes'][1].update(size=-4096)), d, 'packet_1_size')
        self.invalid(result_for(d, mutate=lambda r: r.update(size=total + 4096)), d, f'declared_size_{total + 4096}_differs_from_packets_total_{total}')
        self.invalid(result_for(d, mutate=lambda r: r.update(bytes=total - 1)), d, 'declared_bytes_')
        self.invalid(result_for(d, mutate=lambda r: r.update(packets=65)), d, 'declared_packets_65_differs')
        self.invalid(result_for(d, mutate=lambda r: r.update(address=BASE + 4096)), d, 'first_packet_address_differs_from_declared_address')
        self.invalid(result_for(d), d[:-PKT], 'dump_size_')                                                              # dump of the wrong size
        self.invalid(result_for(d), d + b'\xff', 'dump_size_')
        self.invalid(result_for(d, mutate=lambda r: r['packet_hashes'][4].update(packet=5)), d, 'packet_number_4')

    def test_malformed_hashes_are_invalid(self):
        d = flash()
        good = result_for(d)['packet_hashes'][7]['sha256']
        for bad in (good.upper(), good[:-1], good + '0', 'z' * 64, '', None, 123, ['a'] * 64, ' ' + good[1:]):
            self.invalid(result_for(d, mutate=lambda r, b=bad: r['packet_hashes'][7].update(sha256=b)), d, 'packet_7_malformed_sha256')
        for bad in ('A' * 64, 'abc', None, 0):
            self.invalid(result_for(d, mutate=lambda r, b=bad: r.update(data_sha256=b)), d, 'malformed_global_sha256')
        self.invalid(result_for(d, mutate=lambda r: r.pop('data_sha256')), d, 'malformed_global_sha256')

    def test_no_packet_hashes_or_wrong_shapes_are_invalid(self):
        d = flash()
        self.invalid(result_for(d, mutate=lambda r: r.pop('packet_hashes')), d, 'no_packet_hashes_in_result')
        self.invalid(result_for(d, mutate=lambda r: r.update(packet_hashes=[])), d, 'no_packet_hashes_in_result')
        self.invalid(result_for(d, mutate=lambda r: r['packet_hashes'].__setitem__(2, 'x')), d, 'packet_2_not_an_object')
        with self.assertRaises(cmp.InvalidEvidence):
            cmp.compare(None, d)

    def test_problems_are_reported_for_the_first_broken_packet_only_not_as_a_cascade(self):
        d = flash()
        text = self.invalid(result_for(d, mutate=lambda r: r['packet_hashes'].pop(3)), d)
        self.assertLess(len(text), 400)

    def test_the_three_outcomes_are_explicit(self):
        ref = flash(); t = bytearray(ref); t[PKT * 2 + 1] ^= 1
        self.assertEqual(cmp.compare(result_for(ref), ref)['status'], 'match')
        self.assertEqual(cmp.compare(result_for(bytes(t)), ref)['status'], 'content_divergence')
        with self.assertRaises(cmp.InvalidEvidence):
            cmp.compare(result_for(ref, mutate=lambda r: r.update(data_sha256='0' * 64)), ref)

    def test_cli_exit_codes_statuses_and_phase_results_format(self):
        import io, contextlib
        ref = flash(); t = bytearray(ref); t[PKT * 2 + 1] ^= 1

        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cmp.main(list(argv))
            out = buf.getvalue()
            return code, (json.loads(out) if out.lstrip().startswith('{') else out)
        with tempfile.TemporaryDirectory() as tmp:
            p = lambda n: os.path.join(tmp, n)
            open(p('ref.bin'), 'wb').write(ref)
            json.dump({'status': 'completed', 'result': result_for(ref)}, open(p('same.json'), 'w'))          # phaseN-results.json shape
            json.dump(result_for(bytes(t)), open(p('diff.json'), 'w'))                                         # bare result shape
            json.dump(result_for(ref, mutate=lambda r: r.update(data_sha256='1' * 64)), open(p('contradiction.json'), 'w'))
            json.dump(result_for(ref, mutate=lambda r: r['packet_hashes'][1].update(ack_bytes=5)), open(p('ack.json'), 'w'))
            open(p('notjson.json'), 'w').write('{nope')
            open(p('list.json'), 'w').write('[1, 2]')
            code, out = run(p('same.json'), p('ref.bin')); self.assertEqual((code, out['status']), (0, 'match'))
            code, out = run(p('diff.json'), p('ref.bin')); self.assertEqual((code, out['status'], out['different_packets']), (1, 'content_divergence', [2]))
            code, out = run(p('contradiction.json'), p('ref.bin')); self.assertEqual((code, out['status']), (2, 'invalid_evidence'))
            self.assertIn('per_packet_hashes_contradict_global_sha256', out['problems'][0])
            code, out = run(p('ack.json'), p('ref.bin')); self.assertEqual((code, out['status']), (2, 'invalid_evidence'))
            for bad in (p('notjson.json'), p('list.json'), p('missing.json')):
                code, out = run(bad, p('ref.bin')); self.assertEqual((code, out['status']), (2, 'invalid_evidence'), bad)
            code, out = run(p('diff.json'), p('missing.bin')); self.assertEqual((code, out['status']), (2, 'invalid_evidence'))
            code, out = run(p('same.json'), p('ref.bin'), '--address', 'zz'); self.assertEqual((code, out['status']), (2, 'invalid_evidence'))
            code, out = run(p('same.json'), p('ref.bin'), '--address', str(BASE)); self.assertEqual((code, out['status']), (0, 'match'))
            code, out = run(p('same.json'), p('ref.bin'), '--address', str(BASE + 1)); self.assertEqual(code, 2)     # wrong base: addresses no longer match
            code, out = run(p('same.json')); self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()

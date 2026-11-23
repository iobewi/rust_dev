"""Fixed T1-T5 plan and offline verdict rules for `diagnostic.p4_flasher_read` (no board, no network).

The rules are written BEFORE the hardware run and are never edited during it. `evaluate()` reads only the structured
operation result (hashes and numbers, never flash data) and an optional private reference table.
Usage: python tools/p4_flasher_read_t1_t5.py plan | evaluate RESULT.json [REFERENCE.json]
"""
import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol import p4_flasher_consent, validate_p4_flasher_plan  # noqa: E402

HISTORICAL_KIB_S = 45.4
A, BOUNDARY, BIG = 0x00200000, 0x01000000, 0x00100000


def _r(address, size, packet):
    return {'op': 'read', 'address': address, 'size': size, 'packet_size': packet}


def _m(address, size):
    return {'op': 'md5', 'address': address, 'size': size}


# (test, step). No `attach`: the stub already attached the NOR at start-up and the mandate does not require a second one.
LABELLED = [
    ('T1', _r(A, 65536, 4096)), ('T1', _r(A, 65536, 4096)), ('T1', _m(A, 65536)),
    ('T2', _r(0x00200100, 8192, 4096)), ('T2', _r(0x00200203, 5001, 1024)), ('T2', _r(0x00200400, 4099, 4096)),
    ('T2', _r(0x00201001, 12289, 2048)), ('T2', _r(0x00201FFF, 1, 1024)), ('T2', _r(0x00202002, 4, 1024)),
    ('T2', _m(0x00200203, 5001)), ('T2', _m(0x00201001, 12289)),
    ('T3', _r(BOUNDARY - 0x1000, 4096, 4096)), ('T3', _r(BOUNDARY, 16384, 4096)), ('T3', _r(BOUNDARY - 0x1000, 8192, 4096)),
    ('T3', _r(BOUNDARY - 7, 22, 1024)), ('T3', _r(BOUNDARY - 0x200, 4096, 1024)),
    ('T3', _m(BOUNDARY - 0x1000, 8192)), ('T3', _m(BOUNDARY, 16384)), ('T3', _m(BOUNDARY - 7, 22)),
    ('T4', _r(BIG, 1 << 20, 4096)), ('T4', _m(BIG, 1 << 20)), ('T5', _r(BIG, 1 << 20, 4096)),
]
PLAN = [step for _, step in LABELLED]
TESTS = [test for test, _ in LABELLED]
validate_p4_flasher_plan(PLAN)


def consent(device, stub_sha256):
    return p4_flasher_consent(device, stub_sha256, PLAN)


def _find(test, op, **where):
    return [i for i, (t, s) in enumerate(LABELLED) if t == test and s['op'] == op and all(s.get(k) == v for k, v in where.items())]


def evaluate(result, reference=None):
    """Return {'tests': {T1..T5: verdict}, 'notes': [...], 'throughput': {...}}. Verdicts: PASS / FAIL / INDETERMINATE."""
    notes, tests = [], {}
    steps = result.get('steps') if isinstance(result, dict) else None
    structural = (isinstance(steps, list) and len(steps) == len(PLAN) and result.get('nor_written') is False
                  and sorted(result.get('opcodes_sent', [])) == [0x13, 0xD2])
    if not structural:
        return {'tests': {t: 'FAIL' if isinstance(steps, list) and steps else 'INDETERMINATE' for t in ('T1', 'T2', 'T3', 'T4', 'T5')},
                'notes': ['structure: unexpected step count, nor_written or opcodes_sent (only 0x13 and 0xd2 are expected)'], 'throughput': {}}
    reads_ok = {}
    for i, (plan, step) in enumerate(zip(PLAN, steps)):
        if plan['op'] == 'read':
            ok = (step.get('op') == 'read' and step.get('bytes') == plan['size'] and step.get('md5_match') is True
                  and step.get('packets') == math.ceil(plan['size'] / plan['packet_size']) and isinstance(step.get('data_sha256'), str))
            reads_ok[i] = ok
        else:
            reads_ok[i] = step.get('op') == 'md5' and isinstance(step.get('md5_stub'), str)

    def same_md5(read_index, md5_index):
        return steps[read_index].get('md5_host') == steps[md5_index].get('md5_stub')

    def group(test):
        return all(reads_ok[i] for i, t in enumerate(TESTS) if t == test)

    t1 = group('T1') and steps[0]['data_sha256'] == steps[1]['data_sha256'] and same_md5(0, 2)
    t2 = group('T2') and same_md5(4, 9) and same_md5(6, 10)
    t3 = group('T3') and same_md5(12, 17) and same_md5(13, 16) and same_md5(14, 18)
    t4 = group('T4') and same_md5(19, 20)
    t5 = reads_ok[21] and steps[21]['data_sha256'] == steps[19]['data_sha256'] and steps[21]['md5_host'] == steps[19]['md5_host']
    tests = {'T1': t1, 'T2': t2, 'T3': t3, 'T4': t4, 'T5': t5}
    verdict = {k: 'PASS' if v else 'FAIL' for k, v in tests.items()}

    # Boundary: an address that wrapped at 2**24 would return the start of the NOR. The reference (private backup, 2026-11-13) says
    # the region around 16 MiB is blank, so the after-boundary capture must NOT equal the capture of the aliased start of the NOR.
    if reference and verdict['T3'] == 'PASS':
        ref = {row['i']: row for row in reference}
        mismatched = [i for i in (11, 12, 13, 14, 15) if ref.get(i) and steps[i]['data_sha256'] != ref[i]['ref_sha256']]
        if mismatched:
            verdict['T3'] = 'INDETERMINATE'
            notes.append(f'T3: internally consistent but different from the 2026-11-13 reference at steps {mismatched}; '
                         'the card content may have changed since: inspect before concluding (a wrap would show non-blank data)')
        else:
            notes.append('T3: all boundary captures equal the reference (blank around 16 MiB, wrap alias is non-blank): no wrap, no shift detectable on blank data')
    if verdict['T3'] == 'PASS' and not reference:
        # Every path of the stub shares one NOR read routine: on blank data only an external reference can show a wrap or a shift.
        verdict['T3'] = 'INDETERMINATE'
        notes.append('T3: no reference table supplied; internal consistency alone cannot show a wrap at 16 MiB')
    if reference:
        ref = {row['i']: row for row in reference}
        agree = [i for i, s in enumerate(steps) if s.get('op') == 'read' and ref.get(i) and s['data_sha256'] == ref[i]['ref_sha256']]
        differ = [i for i, s in enumerate(steps) if s.get('op') == 'read' and ref.get(i) and s['data_sha256'] != ref[i]['ref_sha256']]
        notes.append(f'reference (not independent of time): {len(agree)} read(s) equal, {len(differ)} different {differ}')
    rates = {t: steps[i].get('throughput_kib_s') for t, i in (('T4', 19), ('T5', 21)) if isinstance(steps[i].get('throughput_kib_s'), (int, float))}
    mean = sum(rates.values()) / len(rates) if rates else None
    throughput = {'T4_kib_s': rates.get('T4'), 'T5_kib_s': rates.get('T5'), 'mean_kib_s': mean, 'historical_kib_s': HISTORICAL_KIB_S,
                  'ratio_to_historical': mean / HISTORICAL_KIB_S if mean else None, 'volume_bytes': 1 << 20}
    return {'tests': verdict, 'notes': notes, 'throughput': throughput}


def main(argv):
    if len(argv) >= 2 and argv[1] == 'plan':
        print(json.dumps({'plan': PLAN, 'tests': TESTS}, indent=1))
        return 0
    if len(argv) >= 3 and argv[1] == 'evaluate':
        result = json.loads(Path(argv[2]).read_text())
        result = result.get('result', result)
        reference = json.loads(Path(argv[3]).read_text()) if len(argv) > 3 else None
        print(json.dumps(evaluate(result, reference), indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))

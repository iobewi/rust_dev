"""Single-shot client for `diagnostic.p4_flasher_read` with the fixed T1-T5 plan (model: the A-5/B bring-up runs).

Offline by default: without --execute it only builds and validates the request. A hardware run needs --execute AND the operator's
authorization string in IDLAB_FLASHER_AUTHORIZED (= the SHA-256 of the plan). One command, one submission, never a retry, no reset.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p4_flasher_read_t1_t5 as t15  # noqa: E402
from protocol import P4_FLASHER_READ, p4_flasher_plan_canonical, validate_command  # noqa: E402

COMMAND_TIMEOUT, LEASE_TTL, MIN_GRANT_SECONDS = 480, 600, 420
ROM_ONLY_CAPS = {'identify', 'mark', 'diagnostic.p4_usb_trace', 'diagnostic.p4_usb_bringup', P4_FLASHER_READ}


class Refusal(Exception):
    """A precondition failed BEFORE anything was submitted."""


def plan_sha256():
    return hashlib.sha256(p4_flasher_plan_canonical(t15.PLAN).encode()).hexdigest()


def build_args(device_id, stub_sha256, artifact_id):
    return {'chip': 'esp32p4', 'scenario': 'read_only_v1', 'stub_artifact': artifact_id, 'stub_sha256': stub_sha256,
            'plan': t15.PLAN, 'consent': t15.consent(device_id, stub_sha256)}


def prechecks(health, device, expected):
    """Strict comparison with the values the operator authorized. Returns the record to archive."""
    record = {'epoch': health['epoch'], 'git': health['build']['git_sha'], 'generation': device['generation'],
              'connected': device['connected'], 'caps': device['capabilities'],
              'transport': (device.get('provenance') or {}).get('transport'), 'quarantined': device['quarantined'],
              'resident': device.get('diagnostic_resident'), 'reservation': device['reservation'],
              'privileged_seconds': device['privileged_seconds']}
    problems = []
    for key, name in (('git', 'git_sha'), ('epoch', 'epoch'), ('generation', 'generation')):
        if record[key] != expected[name]:
            problems.append(f'{key} {record[key]!r} != authorized {expected[name]!r}')
    if not record['connected']:
        problems.append('board not connected')
    if record['quarantined'] or record['resident']:
        problems.append('board quarantined or diagnostic_resident: local ROM recovery required first')
    if record['reservation'] is not None:
        problems.append('board already reserved')
    if P4_FLASHER_READ not in record['caps'] or not set(record['caps']) <= ROM_ONLY_CAPS:
        problems.append(f'capabilities are not a ROM-only session offering the flasher read: {record["caps"]}')
    if record['transport'] != 'native_usb_other':
        problems.append('transport is not the native USB OTG')
    if not isinstance(record['privileged_seconds'], (int, float)) or record['privileged_seconds'] < MIN_GRANT_SECONDS:
        problems.append(f'local grant must cover the run (>= {MIN_GRANT_SECONDS} s)')
    if problems:
        raise Refusal('; '.join(problems))
    return record


def dump(path, value):
    path.write_text(json.dumps(value, indent=1, default=str))


def seal(out):
    lines = [f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}' for p in sorted(out.iterdir()) if p.is_file() and p.name != 'SHA256SUMS']
    (out / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')


def run(client, expected, stub_path, out, reference=None, log=lambda **k: None):
    """Exactly one submission. `client` is a BenchClient (or a test double)."""
    stub_path, out = Path(stub_path), Path(out)
    stub_sha = hashlib.sha256(stub_path.read_bytes()).hexdigest()
    if stub_sha != expected['stub_sha256']:
        raise Refusal('stub JSON differs from the authorized candidate')
    if plan_sha256() != expected['plan_sha256']:
        raise Refusal('plan differs from the authorized plan')
    out.mkdir(mode=0o700)                                    # a NEW evidence directory per run; an existing one is refused
    pre = prechecks(client.health(), client.device(expected['device']), expected)
    dump(out / 'prechecks.json', pre); log(step='prechecks', **pre)
    # Artifacts left by earlier (possibly uncertain) work are retained and NEVER touched: they are only recorded.
    preexisting = [{'id': a.get('id'), 'sha256': a.get('sha256')} for a in client.artifacts()['artifacts']]
    dump(out / 'preexisting-artifacts.json', preexisting)
    art = client.upload(stub_path)
    if art['sha256'] != stub_sha:
        raise Refusal('uploaded artifact digest mismatch')
    if art['id'] in {a['id'] for a in preexisting}:
        raise Refusal('the upload returned a pre-existing artifact id: inspect before submitting')
    log(step='artifact', id=art['id'], size=art['size'])
    board = client.reserve(expected['device'], ttl=LEASE_TTL, allow_destructive=True, output_dir=out)
    log(step='reserved', epoch=board.epoch, generation=board.generation)
    # BenchClient.reserve() re-reads the epoch and generation and builds the Board with the CURRENT values: a board that reconnected
    # between the prechecks and the reservation would silently become the target. Compare again, release and stop BEFORE any command.
    if board.epoch != expected['epoch'] or board.generation != expected['generation']:
        try:
            board.close()
            released = True
        except Exception as error:
            released = False
            log(step='release_error', error=str(error)[:200])
        dump(out / 'refused-after-reservation.json', {'epoch': board.epoch, 'generation': board.generation, 'released': released})
        seal(out)
        raise Refusal(f'target changed after the reservation (epoch {board.epoch!r}, generation {board.generation!r}): nothing submitted')
    args = build_args(expected['device'], stub_sha, art['id'])
    dump(out / 'submitted-args.json', args)
    verdict, result = 'indeterminate', None
    try:
        result = board.command(P4_FLASHER_READ, args, timeout=COMMAND_TIMEOUT, allow_destructive=True)
        dump(out / 'result.json', result); verdict = 'completed'; log(step='result_saved')
    except Exception as error:                               # never retried, never reset
        name = type(error).__name__
        operation = getattr(error, 'operation', None)
        log(step='ERROR', type=name, error=str(error)[:800])
        # A lost POST response is NOT a failure: the client marks the board `uncertain` (or keeps an operation `pending`) and any
        # network error may have started the hardware. Only a definitive, settled refusal or failure is classed `failed`.
        unresolved = name == 'PendingOperation' or getattr(board, 'uncertain', False) is True or bool(getattr(board, 'pending', None))
        verdict = 'indeterminate' if unresolved else 'failed'
        if operation:
            dump(out / 'operation.json', operation)
    try:
        ids = [json.loads(line)['id'] for line in (out / 'operations.jsonl').read_text().splitlines() if line.strip()]
        if ids:
            dump(out / 'operation.json', client.operation(ids[-1])); log(step='operation_saved', id=ids[-1])
    except Exception as error:
        log(step='operation_save_error', error=str(error)[:200])
    if verdict != 'indeterminate':
        try:
            board.close(); log(step='released')
        except Exception as error:
            log(step='release_error', error=str(error)[:200])
    try:
        device = client.device(expected['device'])
        post = {k: device.get(k) for k in ('connected', 'generation', 'quarantined', 'diagnostic_resident', 'reservation', 'privileged_seconds', 'capabilities')}
        dump(out / 'post.json', post); log(step='post', **post)
    except Exception as error:
        log(step='post_error', error=str(error)[:200])
    if verdict == 'completed':
        evaluation = t15.evaluate(result.get('result', result) if isinstance(result, dict) else result, reference)
        dump(out / 'evaluation.json', evaluation); log(step='evaluation', **evaluation)
    seal(out)
    log(step='verdict', v=verdict)
    return verdict


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', default=os.environ.get('BENCH_URL', 'http://127.0.0.1:8080'))
    ap.add_argument('--tokens', default=os.environ.get('BENCH_TOKENS_FILE', str(Path.home() / '.local/state/iobewi-bench/tokens.json')))
    ap.add_argument('--device', default='esp32-local')
    ap.add_argument('--stub', required=True)
    ap.add_argument('--stub-sha256', required=True)
    ap.add_argument('--git-sha', required=True)
    ap.add_argument('--epoch', required=True)
    ap.add_argument('--generation', required=True)
    ap.add_argument('--reference', help='private reference table (JSON); without it T3 stays INDETERMINATE')
    ap.add_argument('--out', required=True, help='NEW private evidence directory')
    ap.add_argument('--execute', action='store_true')
    opts = ap.parse_args(argv)
    expected = {'device': opts.device, 'stub_sha256': opts.stub_sha256, 'plan_sha256': plan_sha256(),
                'git_sha': opts.git_sha, 'epoch': opts.epoch, 'generation': opts.generation}
    log = lambda **k: print(json.dumps(k, default=str), flush=True)  # noqa: E731
    log(step='request', **{k: v for k, v in expected.items()}, steps=len(t15.PLAN))
    if not opts.execute:
        print('dry run: request built, nothing submitted')
        return 0
    if os.environ.get('IDLAB_FLASHER_AUTHORIZED') != expected['plan_sha256']:
        print('refused: set IDLAB_FLASHER_AUTHORIZED to the plan SHA-256 given by the operator', file=sys.stderr)
        return 2
    from iobewi_bench import BenchClient
    config = json.loads(Path(opts.tokens).read_text())
    token = next(k for k, v in config.items() if v.get('role') == 'client')
    reference = json.loads(Path(opts.reference).read_text()) if opts.reference else None
    try:
        return 0 if run(BenchClient(opts.url, token), expected, opts.stub, opts.out, reference, log) == 'completed' else 1
    except Refusal as error:
        print(f'refused before submission: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Compares the three builds (baseline, patched/macro off, patched/macro on): hashes, JSON fields, sections, symbols, bytes changed and where."""
import base64, collections, hashlib, json, re, subprocess, sys
W = sys.argv[1]
ELF = {'baseline': f'{W}/base/build-esp32p4/src/stub-esp32p4.elf', 'neutral': f'{W}/variant/build-neutral/src/stub-esp32p4.elf',
       'no-boost': f'{W}/variant/build-noboost/src/stub-esp32p4.elf'}
JS = {'baseline': f'{W}/base/build-esp32p4/esp32p4.json', 'neutral': f'{W}/variant/build-neutral/esp32p4.json', 'no-boost': f'{W}/variant/build-noboost/esp32p4.json'}
tool = lambda *a: subprocess.run(['riscv32-esp-elf-' + a[0], *a[1:]], capture_output=True, text=True, check=True).stdout
sha = lambda p: hashlib.sha256(open(p, 'rb').read()).hexdigest()
js = {k: json.load(open(v)) for k, v in JS.items()}
print('ELF sha256     :', {k: sha(v)[:16] for k, v in ELF.items()})
print('JSON text sha  :', {k: hashlib.sha256(d['text'].encode()).hexdigest()[:16] for k, d in js.items()})
print('JSON data sha  :', {k: hashlib.sha256(d['data'].encode()).hexdigest()[:16] for k, d in js.items()})
print('JSON layout    :', {k: (d['entry'], d['text_start'], d['data_start'], d['bss_start']) for k, d in js.items()})
assert sha(ELF['baseline']) == sha(ELF['neutral']) and js['baseline'] == js['neutral'], 'the patch is NOT neutral when the macro is off'
print('NEUTRALITY     : patched tree with the macro OFF is identical to the baseline (ELF and JSON)')
for f in ('entry', 'text_start', 'data_start', 'bss_start', 'data'):
    assert js['baseline'][f] == js['no-boost'][f], f
print('LAYOUT         : entry, text_start, data, data_start, bss_start identical; only the text bytes differ')
sizes = lambda p: tool('size', '-A', p).splitlines()[1:]
print('SECTION SIZES  :', 'identical' if [l.split()[:2] for l in sizes(ELF['baseline'])] == [l.split()[:2] for l in sizes(ELF['no-boost'])] else 'DIFFERENT')
nm = lambda p: tool('nm', '-S', '--defined-only', '-n', p)
print('SYMBOLS        :', 'identical (names, addresses, sizes)' if nm(ELF['baseline']) == nm(ELF['no-boost']) else 'DIFFERENT')
for k in ('baseline', 'no-boost'):
    tool('objcopy', '-O', 'binary', '-j', '.text', ELF[k], f'{W}/text-{k}.bin')
a, b = (open(f'{W}/text-{k}.bin', 'rb').read() for k in ('baseline', 'no-boost'))
m = re.search(r'\.text\s+([0-9a-f]+)\s+([0-9a-f]+)', tool('objdump', '-h', ELF['baseline'])); vma = int(m.group(2), 16)
syms = sorted((int(p[0], 16), int(p[1], 16), p[3]) for p in (l.split() for l in nm(ELF['baseline']).splitlines()) if len(p) == 4 and p[2].lower() == 't')
where = collections.Counter(next((n for s, z, n in syms if s <= vma + i < s + z), '?') for i, (x, y) in enumerate(zip(a, b)) if x != y)
print(f'BYTES CHANGED  : {sum(where.values())} of {len(a)} .text bytes, all inside: {dict(where)}')
def dis(k): return tool('objdump', '-d', '--disassemble=esp_main', ELF[k])
ins = lambda t: [l for l in t.splitlines() if re.match(r'\s*[0-9a-f]+:\s', l)]
call = lambda t: collections.Counter(re.sub(r'.*(jalr|jal)\s+.*<([^>]+)>.*', r'\2', l) for l in ins(t) if re.search(r'\bjalr?\b.*<', l))
print(f"esp_main       : {len(ins(dis('baseline')))} instructions (baseline) / {len(ins(dis('no-boost')))} (no-boost); same call multiset: {call(dis('baseline')) == call(dis('no-boost'))}")

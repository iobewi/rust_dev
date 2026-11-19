// Agent-side qualification of the P4 USB OTG diagnostic against a SIMULATED P4 (no board, no Web Serial).
// A pass here proves the orchestration contract only; it is never a hardware result.
import test from 'node:test';
import assert from 'node:assert/strict';
import { EspEngine } from '../web/esp-engine.js';
import { P4_TRACE, P4_FIXED, expectedConsent, validateDiagnosticArgs, runP4UsbTrace } from '../web/p4-diagnostic.js';
import { sha256 } from '../web/protocol.js';
import { IDLAB_GET_FLASH_TRACE, IDLAB_READ_FLASH_TRACE } from '../web/p4-stub-trace.js';
import { FORBIDDEN_FLASH_METHODS } from '../web/p4-stub-loader.js';

const b64 = bytes => Buffer.from(bytes).toString('base64');
const stubText = JSON.stringify({ entry: 0x4ff50010, text: b64(new Uint8Array(100).fill(0x13)), text_start: 0x4ff50000,
  data: b64(new Uint8Array(40).fill(7)), data_start: 0x4ff60040, bss_start: 0x4ff60000 });
const stubBytes = new TextEncoder().encode(stubText);
const STUB_SHA = await sha256(stubBytes);

function trace(packets = 256, over = {}) {
  const b = new Uint8Array(64), v = new DataView(b.buffer);
  v.setUint32(0, 0x31544449, true); v.setUint32(4, 1, true);
  [100n, 200n, 4000n, 900n, 3000n].forEach((n, i) => v.setBigUint64(8 + i * 8, n * 1000n, true));
  [7, packets, packets, packets * 4096].forEach((n, i) => v.setUint32(48 + i * 4, n, true));
  for (const [offset, n] of Object.entries(over)) v.setUint32(Number(offset), n, true);
  return b;
}

/** A simulated ESP32-P4 behind an esptool-js-like loader. `fault` selects where the scenario breaks. */
function simP4({ fault = null, chip = 'ESP32-P4', revision = 302, isStub = false } = {}) {
  const calls = [];
  let reads = 0;
  const loader = {
    IS_STUB: isStub, ESP_RAM_BLOCK: 64, DEFAULT_TIMEOUT: 1000, calls,
    chip: { CHIP_NAME: chip, getChipRevision: async () => revision },
    memBegin: async (...a) => { calls.push(['memBegin', ...a]); if (fault === 'memBegin') throw new Error('lost'); },
    memBlock: async (bytes, seq) => { calls.push(['memBlock', seq]); if (fault === 'load_truncated' && seq === 0) throw new Error('truncated'); },
    memFinish: async entry => { calls.push(['memFinish', entry]); },
    async checkCommand(_l, op) {
      calls.push(['cmd', op]);
      if (op === IDLAB_GET_FLASH_TRACE) {
        if (fault === 'probe_rejected') throw new Error('stock stub');
        if (fault === 'bad_counters' && calls.filter(c => c[1] === op).length > 1) return trace(256, { 56: 255 });
        if (fault === 'bad_idt1' && calls.filter(c => c[1] === op).length > 1) return new Uint8Array(64);
        return trace();
      }
      return 0;
    },
    transport: { async read() {
      calls.push(['read']);
      if (!loader.IS_STUB) return Uint8Array.from('OHAI', c => c.charCodeAt(0));
      reads += 1;
      if (fault === 'read_interrupted' && reads === 40) throw new Error('usb disconnected');
      if (fault === 'bad_md5' && reads === 257) return new Uint8Array(3);
      return reads <= 256 ? new Uint8Array(4096).fill(reads & 255) : new Uint8Array(16).fill(0xab);
    }, async write() { calls.push(['ack']); } },
  };
  for (const name of FORBIDDEN_FLASH_METHODS) loader[name] = async () => { calls.push(['FORBIDDEN', name]); throw new Error(name); };
  return loader;
}

const device = { id: 'p4', chip: 'esp32p4', flash_size: 32 * 1024 * 1024 };
const goodArgs = (over = {}) => ({ chip: 'esp32p4', stub_artifact: 'stub', stub_sha256: STUB_SHA, ...P4_FIXED, chip_: undefined,
  consent: expectedConsent('p4', STUB_SHA), ...over });
const clean = a => { const x = { ...a }; delete x.chip_; return x; };
const writes = l => l.calls.filter(c => ['memBegin', 'memBlock', 'memFinish'].includes(c[0]));
const noForbidden = l => assert.equal(l.calls.filter(c => c[0] === 'FORBIDDEN').length, 0);

// ------------------------------------------------------------------ local validation (UI/server are not trusted)
test('every deviation from the fixed request is refused locally with nothing sent to the chip', async () => {
  const bad = [{ address: 0 }, { size: 4096 }, { packet_size: 1024 }, { cpu_hz: 160000000 }, { chip: 'esp32s3' },
    { stub_sha256: 'A'.repeat(64) }, { extra: 1 }, { consent: null }, { consent: { ...expectedConsent('p4', STUB_SHA), device: 'c6' } },
    { consent: { ...expectedConsent('p4', STUB_SHA), stub_sha256: '0'.repeat(64) } },
    { consent: { ...expectedConsent('p4', STUB_SHA), ram_code_execution: 1 } },
    { consent: { ...expectedConsent('p4', STUB_SHA), size: 4096 } },
    { consent: { ...expectedConsent('p4', STUB_SHA), extra: true } }];
  for (const over of bad) {
    const loader = simP4();
    await assert.rejects(runP4UsbTrace({ loader, args: clean(goodArgs(over)), device, stubBytes }),
      e => e.indeterminate === false && /^diagnostic_/.test(e.code), JSON.stringify(over).slice(0, 60));
    assert.equal(loader.calls.length, 0);
  }
  assert.throws(() => validateDiagnosticArgs(clean(goodArgs()), { ...device, chip: 'esp32c6' }), e => e.code === 'diagnostic_chip_unsupported');
});

test('a stub whose bytes do not match the announced SHA-256 never reaches the chip', async () => {
  const loader = simP4();
  await assert.rejects(runP4UsbTrace({ loader, args: clean(goodArgs()), device, stubBytes: new TextEncoder().encode(stubText + ' ') }),
    e => e.code === 'artifact_integrity_failed' && e.indeterminate === false);
  assert.equal(loader.calls.length, 0);
});

// ------------------------------------------------------------------ orchestrator: loader + reader
test('nominal run: RAM load, 256 packets with cumulative ACKs, MD5, IDT1, raw cycles, assumed clock flagged', async () => {
  const loader = simP4(), steps = [];
  const { result } = await runP4UsbTrace({ loader, args: clean(goodArgs()), device, stubBytes, report: d => steps.push(d.phase) });
  assert.equal(result.packets, 256); assert.equal(result.acks, 256); assert.equal(result.bytes, 1048576);
  assert.equal(result.md5, 'ab'.repeat(16)); assert.match(result.data_sha256, /^[0-9a-f]{64}$/);
  assert.equal(result.trace.flash_cycles, '100000'); assert.equal(result.converted.cycles.fill_cycles, '3000000');
  assert.equal(result.cpu_hz.basis, 'assumed_from_stub_code_not_measured'); assert.equal(result.cpu_hz.value, 200000000);
  assert.equal(result.converted.microseconds.send_without_fill, (4000000 - 3000000) / 200);   // fill is inside send
  assert.equal(result.nor_written, false); assert.equal(result.stub_resident, true); assert.equal(result.requires_operator_reset, true);
  assert.equal(loader.calls.filter(c => c[0] === 'ack').length, 256);
  assert.ok(steps.includes('diagnostic_stub_load') && steps.includes('diagnostic_read'));
  assert.equal(loader.calls.filter(c => c[1] === IDLAB_READ_FLASH_TRACE).length, 1);
  noForbidden(loader);
});

test('loader refusals before any write are not indeterminate: wrong chip, old revision, standard stub already running', async () => {
  for (const options of [{ chip: 'ESP32-S3' }, { revision: 200 }, { isStub: true }]) {
    const loader = simP4(options);
    await assert.rejects(runP4UsbTrace({ loader, args: clean(goodArgs()), device, stubBytes }), e => e.indeterminate === false);
    assert.equal(writes(loader).length, 0); noForbidden(loader);
  }
});

test('failures after the first RAM write are indeterminate and never retried', async () => {
  for (const fault of ['memBegin', 'load_truncated', 'probe_rejected', 'read_interrupted', 'bad_md5', 'bad_counters', 'bad_idt1']) {
    const loader = simP4({ fault });
    await assert.rejects(runP4UsbTrace({ loader, args: clean(goodArgs()), device, stubBytes }), e => e.indeterminate === true, fault);
    assert.ok(loader.calls.filter(c => c[0] === 'memBegin').length <= 2, 'no retry (one text + one data segment): ' + fault);
    assert.ok(loader.calls.filter(c => c[1] === IDLAB_READ_FLASH_TRACE).length <= 1);
    noForbidden(loader);
  }
});

test('losing the lease or the grant while reading is indeterminate (the stub is already running)', async () => {
  const loader = simP4(); let n = 0;
  await assert.rejects(runP4UsbTrace({ loader, args: clean(goodArgs()), device, stubBytes,
    check: () => { if (++n > 3) throw Object.assign(new Error('x'), { code: 'reservation_expired' }); } }), e => e.indeterminate === true);
});

// ------------------------------------------------------------------ engine: hold, capability, isolation
async function engineWith(loader, { manual = true, desc = 'ESP32-P4 (revision v3.2)', native = true, jtag = false } = {}) {
  const events = [], holds = [];
  const engine = new EspEngine({ getInfo: () => ({}) }, {}, (n, d) => events.push([n, d]), v => holds.push(v));
  Object.assign(engine, { native, usbSerialJtag: jtag, manualDownload: manual, chipDescription: desc });
  engine.transport = { disconnect: async () => {}, stats: {} };
  engine.openRomSession = async () => { engine.loader = loader; engine.mode = 'loader'; return loader; };
  let completion = null, opened = 0;
  engine.openConsole = async () => { opened++; };
  const context = { check() {}, live: () => true, device, download: async () => stubBytes, upload: async () => ({}), deferCompletion: true,
    complete: v => { completion = v; }, report() {} };
  return { engine, events, holds, context, completion: () => completion, consoleOpens: () => opened };
}

test('the capability exists only for a native USB OTG P4 with the operator-confirmed manual download entry', async () => {
  const l = simP4();
  assert.ok((await engineWith(l)).engine.capabilities().includes(P4_TRACE));
  for (const options of [{ manual: false }, { desc: 'ESP32-S3' }, { native: false }, { jtag: true }])
    assert.ok(!(await engineWith(l, options)).engine.capabilities().includes(P4_TRACE), JSON.stringify(options));
});

test('engine nominal: hold raised before the first write, resident, console never reopened, only mark accepted afterwards', async () => {
  const loader = simP4(), x = await engineWith(loader);
  const result = await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);
  assert.equal(result.md5, 'ab'.repeat(16));
  assert.equal(x.engine.hold, true); assert.equal(x.engine.diagnosticResident, true);
  assert.equal(x.holds[0], true); assert.deepEqual(x.completion(), { hold: true, returnConsole: false });
  assert.equal(x.consoleOpens(), 0);
  for (const command of ['identify', 'partitions', 'flash.read', 'flash.write', P4_TRACE])
    await assert.rejects(x.engine.execute(command, command === P4_TRACE ? clean(goodArgs()) : { address: 0, size: 4096 }, x.context),
      e => e.code === 'operator_recovery_required', command);
  assert.equal((await x.engine.execute('mark', { label: 'after' }, x.context)).label, 'after');
});

test('operator recovery clears the resident flag and every handle of the diagnostic session', async () => {
  const loader = simP4(), x = await engineWith(loader);
  await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);
  x.engine.setHold(false);
  assert.equal(x.engine.diagnosticResident, false); assert.equal(x.engine.loader, null); assert.equal(x.holds.at(-1), false);
});

test('engine refuses without any hold change when the request is invalid or the stub is tampered', async () => {
  for (const [args, download] of [[clean(goodArgs({ address: 0 })), stubBytes], [clean(goodArgs()), new TextEncoder().encode(stubText + ' ')]]) {
    const loader = simP4(), x = await engineWith(loader);
    x.context.download = async () => download;
    await assert.rejects(x.engine.execute(P4_TRACE, args, x.context));
    assert.equal(x.engine.hold, false); assert.equal(x.engine.diagnosticResident, false); assert.equal(writes(loader).length, 0);
  }
});

test('a standard stub still running (operator did not enter ROM mode) is refused and leaves no hold', async () => {
  const loader = simP4({ isStub: true }), x = await engineWith(loader);
  await assert.rejects(x.engine.execute(P4_TRACE, clean(goodArgs()), x.context), e => /requires_a_rom_session/.test(e.code));
  assert.equal(x.engine.hold, false); assert.equal(x.engine.diagnosticResident, false); assert.equal(writes(loader).length, 0);
});

test('interruption during load or read: indeterminate, hold kept, resident, no reset, no console, no retry', async () => {
  for (const fault of ['load_truncated', 'read_interrupted', 'bad_md5']) {
    const loader = simP4({ fault }), x = await engineWith(loader);
    const result = await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);
    assert.equal(result.state, 'indeterminate'); assert.equal(result.hold, true); assert.equal(result.error.code, 'hardware_outcome_unknown');
    assert.equal(x.engine.hold, true); assert.equal(x.engine.diagnosticResident, true); assert.equal(x.consoleOpens(), 0);
    assert.ok(loader.calls.filter(c => c[0] === 'memBegin').length <= 2, 'one load pass only');
    noForbidden(loader);
  }
});

test('manual download is required: without it the engine refuses before opening anything', async () => {
  const x = await engineWith(simP4(), { manual: false });
  await assert.rejects(x.engine.execute(P4_TRACE, clean(goodArgs()), x.context), e => e.code === 'diagnostic_unavailable');
  assert.equal(x.engine.hold, false);
});

test('a standard flash.read on another engine instance (another board) is unaffected by a resident diagnostic', async () => {
  const x = await engineWith(simP4()), other = await engineWith(simP4());
  await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);
  other.engine.identity = async () => ({ chip: 'esp32c6', flash_size: 4194304 });
  other.engine.loader = { readFlash: async (_a, size) => new Uint8Array(size).fill(1) };
  other.context.device = { id: 'c6', chip: 'esp32c6', flash_size: 4194304 };
  const result = await other.engine.execute('flash.read', { address: 0, size: 4096, return_console: false }, other.context);
  assert.equal(result.address, 0); assert.equal(other.engine.diagnosticResident, false);
});

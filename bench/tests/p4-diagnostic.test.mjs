// Agent-side qualification of the P4 USB OTG diagnostic against a SIMULATED P4 (no board, no Web Serial).
// A pass here proves the orchestration contract only; it is never a hardware result.
import test from 'node:test';
import assert from 'node:assert/strict';
import { EspEngine } from '../web/esp-engine.js';
import { P4_TRACE, P4_FIXED, expectedConsent, validateDiagnosticArgs, runP4UsbTrace } from '../web/p4-diagnostic.js';
import { sha256 } from '../web/protocol.js';
import { md5hex } from '../web/md5.js';
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
const fill = n => new Uint8Array(4096).fill(n & 255);
const all = () => { const x = new Uint8Array(256 * 4096); for (let i = 1; i <= 256; i++) x.set(fill(i), (i - 1) * 4096); return x; };
const MD5_ALL = md5hex(all());

function simP4({ fault = null, chip = 'ESP32-P4', revision = 302, isStub = false } = {}) {
  const calls = [];
  let reads = 0;
  const loader = {
    IS_STUB: isStub, ESP_RAM_BLOCK: 0x1800, DEFAULT_TIMEOUT: 1000, calls,
    usesUsbOtg: async () => { calls.push(['usesUsbOtg']); return true; },   // VID 303a / PID 0012 on the bench board
    chip: { CHIP_NAME: chip, USB_RAM_BLOCK: 0x800, getChipRevision: async () => revision },
    // esptool-js 0.7.0: a ROM answers an ESP_SYNC (0x08) eight times with a non-zero val, a stub once with 0; sync() throws on a stub
    syncAnswers: () => loader.IS_STUB === true ? [0] : [1, 1, 1, 1, 1, 1, 1, 1], pending: [],
    sync: async () => { calls.push(['sync']); loader.syncStubDetected = loader.IS_STUB === true; if (loader.syncStubDetected) throw new Error('invalid response'); },
    command: async (op) => { calls.push(['sync_frame', op]); loader.pending = [...loader.syncAnswers()]; return [loader.pending.shift(), new Uint8Array(0)]; },
    readPacket: async () => { if (!loader.pending.length) throw new Error('no data'); return [loader.pending.shift(), new Uint8Array(0)]; },
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
      if (fault === 'corrupt_data' && reads === 10) { const c = fill(reads); c[5] ^= 1; return c; }
      if (reads <= 256) return fill(reads);
      return Uint8Array.from(md5hex(all()).match(/../g), h => parseInt(h, 16));
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
    await assert.rejects(runP4UsbTrace({ loader, romOnly: true, args: clean(goodArgs(over)), device, stubBytes }),
      e => e.indeterminate === false && /^diagnostic_/.test(e.code), JSON.stringify(over).slice(0, 60));
    assert.equal(loader.calls.length, 0);
  }
  assert.throws(() => validateDiagnosticArgs(clean(goodArgs()), { ...device, chip: 'esp32c6' }), e => e.code === 'diagnostic_chip_unsupported');
});

test('a stub whose bytes do not match the announced SHA-256 never reaches the chip', async () => {
  const loader = simP4();
  await assert.rejects(runP4UsbTrace({ loader, romOnly: true, args: clean(goodArgs()), device, stubBytes: new TextEncoder().encode(stubText + ' ') }),
    e => e.code === 'artifact_integrity_failed' && e.indeterminate === false);
  assert.equal(loader.calls.length, 0);
});

// ------------------------------------------------------------------ orchestrator: loader + reader
test('nominal run: RAM load, 256 packets with cumulative ACKs, MD5, IDT1, raw cycles, assumed clock flagged', async () => {
  const loader = simP4(), steps = [];
  const { result } = await runP4UsbTrace({ loader, romOnly: true, args: clean(goodArgs()), device, stubBytes, report: d => steps.push(d.phase) });
  assert.equal(result.packets, 256); assert.equal(result.acks, 256); assert.equal(result.bytes, 1048576);
  assert.equal(result.md5, MD5_ALL); assert.match(result.data_sha256, /^[0-9a-f]{64}$/);
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
    await assert.rejects(runP4UsbTrace({ loader, romOnly: true, args: clean(goodArgs()), device, stubBytes }), e => e.indeterminate === false);
    assert.equal(writes(loader).length, 0); noForbidden(loader);
  }
});

test('failures after the first RAM write are indeterminate and never retried', async () => {
  for (const fault of ['memBegin', 'load_truncated', 'probe_rejected', 'read_interrupted', 'bad_md5', 'corrupt_data', 'bad_counters', 'bad_idt1']) {
    const loader = simP4({ fault });
    await assert.rejects(runP4UsbTrace({ loader, romOnly: true, args: clean(goodArgs()), device, stubBytes }), e => e.indeterminate === true, fault);
    assert.ok(loader.calls.filter(c => c[0] === 'memBegin').length <= 2, 'no retry (one text + one data segment): ' + fault);
    assert.ok(loader.calls.filter(c => c[1] === IDLAB_READ_FLASH_TRACE).length <= 1);
    noForbidden(loader);
  }
});

test('losing the lease or the grant while reading is indeterminate (the stub is already running)', async () => {
  const loader = simP4(); let n = 0;
  await assert.rejects(runP4UsbTrace({ loader, romOnly: true, args: clean(goodArgs()), device, stubBytes,
    check: () => { if (++n > 3) throw Object.assign(new Error('x'), { code: 'reservation_expired' }); } }), e => e.indeterminate === true);
});

// ------------------------------------------------------------------ engine: hold, capability, isolation
async function engineWith(loader, { manual = true, desc = 'ESP32-P4 (revision v3.2)', native = true, jtag = false } = {}) {
  const events = [], holds = [];
  const engine = new EspEngine({ getInfo: () => ({}) }, {}, (n, d) => events.push([n, d]), v => holds.push(v));
  Object.assign(engine, { native, usbSerialJtag: jtag, manualDownload: manual, romOnly: manual, chipDescription: desc });
  engine.transport = { disconnect: async () => {}, stats: {} };
  engine.loader = loader; engine.mode = 'loader';   // the ROM-only connection itself (openLoader returns it)
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
  assert.equal(result.md5, MD5_ALL);
  assert.equal(x.engine.hold, true); assert.equal(x.engine.diagnosticResident, true);
  assert.equal(x.holds[0], true); assert.deepEqual(x.completion(), { hold: true, returnConsole: false });
  assert.equal(x.consoleOpens(), 0);
  for (const command of ['identify', P4_TRACE])
    await assert.rejects(x.engine.execute(command, command === P4_TRACE ? clean(goodArgs()) : {}, x.context),
      e => e.code === 'operator_recovery_required', command);
  for (const command of ['partitions', 'flash.read', 'flash.write', 'flash.erase', 'console', 'uart.write'])   // a ROM-only session has no stub: never offered
    await assert.rejects(x.engine.execute(command, { address: 0, size: 4096 }, x.context), e => e.code === 'diagnostic_rom_only_session', command);
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
  await assert.rejects(x.engine.execute(P4_TRACE, clean(goodArgs()), x.context), e => e.code === 'diagnostic_stub_reported_running');
  assert.equal(x.engine.hold, false); assert.equal(x.engine.diagnosticResident, false); assert.equal(writes(loader).length, 0);
});

test('interruption during load or read: indeterminate, hold kept, resident, no reset, no console, no retry', async () => {
  for (const fault of ['load_truncated', 'read_interrupted', 'bad_md5', 'corrupt_data']) {
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
  const x = await engineWith(simP4()), other = await engineWith(simP4(), { manual: false, desc: 'ESP32-C6', native: false });
  await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);
  other.engine.identity = async () => ({ chip: 'esp32c6', flash_size: 4194304 });
  other.engine.loader = { readFlash: async (_a, size) => new Uint8Array(size).fill(1) };
  other.context.device = { id: 'c6', chip: 'esp32c6', flash_size: 4194304 };
  const result = await other.engine.execute('flash.read', { address: 0, size: 4096, return_console: false }, other.context);
  assert.equal(result.address, 0); assert.equal(other.engine.diagnosticResident, false);
});

test('ROM-return proof: a stub-free ROM sync proves it; a still-running stub or another chip does not; nothing is written', async () => {
  for (const [options, ok] of [[{}, true], [{ isStub: true }, false], [{ chip: 'ESP32-S3' }, false]]) {
    const loader = simP4(options), x = await engineWith(loader);
    x.engine.close = async () => {};
    const outcome = x.engine.verifyRomReturn();
    if (ok) assert.equal(await outcome, true); else await assert.rejects(outcome);
    assert.equal(writes(loader).length, 0); noForbidden(loader);
  }
});

test('agent recovery after the diagnostic carries the engine proof; without a resident stub it carries none; a failed proof sends nothing', async () => {
  const { Agent } = await import('../web/agent.js');
  const make = resident => {
    const sent = [], engine = { diagnosticResident: resident, verifyRomReturn: async () => { if (resident === 'fail') throw new Error('still running'); return true; } };
    const agent = Object.create(Agent.prototype);
    Object.assign(agent, { engine, active: null, awaitingResult: null, event: (name, data) => sent.push([name, data]) });
    return { agent, sent };
  };
  let x = make(true); await x.agent.recover(); assert.deepEqual(x.sent, [['recover', { rom_verified: true }]]);
  x = make(false); await x.agent.recover(); assert.deepEqual(x.sent, [['recover', {}]]);
  x = make('fail'); await assert.rejects(x.agent.recover()); assert.deepEqual(x.sent, []);
});

test('page reload: a fresh engine learns diagnostic_resident from the server registration and then must prove the ROM return', async () => {
  const { Agent } = await import('../web/agent.js');
  const sent = [], engine = new EspEngine({ getInfo: () => ({}) }, {}, () => {}, () => {});   // rebuilt engine: diagnosticResident === false
  assert.equal(engine.diagnosticResident, false);
  let proofs = 0; engine.verifyRomReturn = async () => { proofs++; return true; };
  const agent = Object.create(Agent.prototype);
  Object.assign(agent, { engine, active: null, awaitingResult: null, log() {}, onState() {}, event: (n, d) => sent.push([n, d]) });
  await agent.message({ type: 'registered', device: 'p4', generation: 'g2', quarantined: true, diagnostic_resident: true });
  assert.equal(engine.hold, true); assert.equal(engine.diagnosticResident, true);
  await agent.recover();
  assert.equal(proofs, 1); assert.deepEqual(sent, [['recover', { rom_verified: true }]]);
});

test('registration without the resident flag is never a basis to lift a quarantine', async () => {
  const { Agent } = await import('../web/agent.js');
  const sent = [], engine = new EspEngine({ getInfo: () => ({}) }, {}, () => {}, () => {});
  const agent = Object.create(Agent.prototype);
  Object.assign(agent, { engine, active: null, awaitingResult: null, log() {}, onState() {}, event: (n, d) => sent.push([n, d]) });
  await agent.message({ type: 'registered', device: 'p4', generation: 'g2', quarantined: true });
  await assert.rejects(agent.recover(), /diagnostic_state_unknown/); assert.deepEqual(sent, []);
  await agent.message({ type: 'registered', device: 'p4', generation: 'g3', quarantined: false, diagnostic_resident: false });
  engine.setHold(false); await agent.recover(); assert.deepEqual(sent, [['recover', {}]]);
});

// ------------------------------------------------------------------ error observability (the cause of a failure must reach the operator)
import { hardwareError, diagnostic } from '../web/hardware-errors.js';

const refusal = async (options, mutate = x => x) => {
  const loader = simP4(options), x = mutate(await engineWith(loader));
  let thrown; try { await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context); } catch (e) { thrown = e; }
  return { thrown, loader, x };
};
// what the agent does with whatever the engine throws (second pass through hardwareError)
const published = e => hardwareError(e, false, {});

test('refusal BEFORE the RAM load keeps its exact code, phase, category and "not started" (the observed field failure)', async () => {
  for (const [options, code] of [[{ isStub: true }, 'diagnostic_stub_reported_running'], [{ chip: 'ESP32-S3' }, 'diagnostic_stub_wrong_chip'],
      [{ revision: 200 }, 'diagnostic_stub_unsupported_chip_revision']]) {
    const { thrown, loader, x } = await refusal(options);
    assert.equal(thrown.code, code, 'engine keeps the code');
    const out = published(thrown);                       // second pass: what the server receives
    assert.deepEqual([out.code, out.phase, out.category, out.ram_load, out.origin_code, out.origin_error_type],
      [code, 'diagnostic_stub_load', 'refused_before_load', 'not_started', code, 'IdlabDiagnosticError'], code);
    assert.notEqual(out.code, 'hardware_command_failed'); assert.notEqual(out.error_type, 'Error');
    assert.equal(writes(loader).length, 0); assert.equal(x.engine.hold, false); assert.equal(x.engine.diagnosticResident, false);
    assert.match(diagnostic(out.code), /rien n’a été chargé|session ROM/i);
    if (code.endsWith('revision')) assert.equal(out.chip_revision, 200);
  }
});

test('local validation refusals and manual-download refusal keep their code and are "local_validation / not_started"', async () => {
  const loader = simP4(), x = await engineWith(loader);
  let e; try { await x.engine.execute(P4_TRACE, clean(goodArgs({ address: 0 })), x.context); } catch (err) { e = err; }
  const out = published(e);
  assert.deepEqual([out.code, out.category, out.ram_load, out.phase], ['diagnostic_parameter_not_allowed', 'local_validation', 'not_started', 'diagnostic_stub_load']);
  const y = await engineWith(simP4(), { manual: false }); let f; try { await y.engine.execute(P4_TRACE, clean(goodArgs()), y.context); } catch (err) { f = err; }
  assert.equal(published(f).code, 'diagnostic_unavailable');
  const z = await engineWith(simP4()); z.engine.loader = null; z.engine.openLoader = async () => { throw Object.assign(new Error('x'), { code: 'diagnostic_rom_only_required' }); };
  let g; try { await z.engine.execute(P4_TRACE, clean(goodArgs()), z.context); } catch (err) { g = err; }
  assert.equal(published(g).code, 'diagnostic_rom_only_required');
});

test('failures DURING the RAM load are indeterminate with the origin code, "ram_load_failed" and "started"', async () => {
  for (const [fault, origin] of [['memBegin', 'diagnostic_stub_ram_load_failed'], ['load_truncated', 'diagnostic_stub_ram_load_failed'],
      ['probe_rejected', 'diagnostic_stub_not_identified']]) {
    const { thrown, x } = await refusal({ fault });
    assert.equal(thrown, undefined, 'indeterminate outcomes are returned, not thrown');
    const result = await (async () => { const y = await engineWith(simP4({ fault })); return y.engine.execute(P4_TRACE, clean(goodArgs()), y.context); })();
    assert.equal(result.state, 'indeterminate'); assert.equal(result.error.code, 'hardware_outcome_unknown');
    assert.deepEqual([result.error.origin_code, result.error.category, result.error.ram_load], [origin, 'ram_load_failed', 'started'], fault);
    assert.equal(x.engine.hold, true);
  }
});

test('stream failures after the launch: idlab_trace_* origin, "stream_failed", phase diagnostic_read, still indeterminate', async () => {
  for (const [fault, origin] of [['read_interrupted', 'idlab_trace_stream_failed'], ['corrupt_data', 'idlab_trace_md5_mismatch'], ['bad_md5', 'idlab_trace_stream_failed']]) {
    const x = await engineWith(simP4({ fault })), result = await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);
    assert.equal(result.state, 'indeterminate');
    assert.ok(result.error.origin_code.startsWith('idlab_trace_'), fault + ' ' + result.error.origin_code);
    assert.deepEqual([result.error.category, result.error.ram_load, result.error.phase], ['stream_failed', 'started', 'diagnostic_read']);
    assert.equal(result.error.origin_error_type, 'IdlabTraceError'); assert.equal(x.engine.hold, true); assert.equal(x.engine.diagnosticResident, true);
  }
});

test('an unexpected error after the chip was touched is indeterminate with an UNKNOWN RAM-load state, never a guess', async () => {
  const loader = simP4(); loader.memBlock = async () => { throw new TypeError('boom'); };
  const x = await engineWith(loader), result = await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);
  assert.equal(result.state, 'indeterminate'); assert.equal(result.error.code, 'hardware_outcome_unknown');
  assert.equal(result.error.category, 'ram_load_failed');   // the loader wraps it as a RAM-load failure with sessionIndeterminate
  const unexpected = simP4(); unexpected.chip = { CHIP_NAME: 'ESP32-P4', getChipRevision: async () => { throw new RangeError('eFuse'); } };
  const y = await engineWith(unexpected), r2 = await y.engine.execute(P4_TRACE, clean(goodArgs()), y.context);
  assert.equal(r2.state, 'indeterminate'); assert.deepEqual([r2.error.category, r2.error.ram_load], ['unexpected', 'unknown']);
});

test('no loader text, secret or firmware content ever reaches the published error or the journal fields', async () => {
  const secrets = ['TOKEN=hunter2', 'Bearer abcdef', 'SSID=home-wifi', 'serial 1234-ABCD'];
  const loader = simP4(); loader.memBegin = async () => { throw new Error(secrets.join(' ')); };
  const x = await engineWith(loader), result = await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);
  const wire = JSON.stringify(result);
  for (const s of secrets) assert.ok(!wire.includes(s), s);
  assert.ok(!wire.includes('"cause"'), 'the free-text cause field is gone');
  const hostile = Object.assign(new Error('TOKEN=hunter2'), { code: 'DIAGNOSTIC_X', detail: 'TOKEN=hunter2',
    diagnostic: { category: 'TOKEN=hunter2', ram_load: 'TOKEN=hunter2', origin_code: 'diagnostic_' + 'x'.repeat(200), origin_error_type: 'Bearer abc', phase: 'TOKEN=hunter2' } });
  const out = hardwareError(hostile);
  assert.equal(out.code, 'hardware_command_failed'); assert.ok(!JSON.stringify(out).includes('hunter2'));
  for (const key of ['category', 'ram_load', 'origin_code']) assert.equal(out[key], undefined, key);
  assert.equal(hardwareError(Object.assign(new Error('x'), { code: 'diagnostic_ok_code_1' })).code, 'diagnostic_ok_code_1');
  assert.equal(hardwareError(Object.assign(new Error('x'), { code: 'idlab_trace_md5_mismatch' })).code, 'idlab_trace_md5_mismatch');
  for (const bad of ['diagnostic_', 'Diagnostic_x', 'diagnostic_x y', 'diagnostic_x\n', 'idlab_trace_' + 'a'.repeat(90)])
    assert.equal(hardwareError(Object.assign(new Error('x'), { code: bad })).code, 'hardware_command_failed', JSON.stringify(bad));
});

test('the observed field failure is now attributable: same inputs as operation c27ec324 give a precise, human-readable cause', async () => {
  const { thrown } = await refusal({ isStub: true });          // a ROM-less (stub still running) session, the leading hypothesis, NOT a confirmed cause
  const out = published(thrown);
  assert.equal(out.code, 'diagnostic_stub_reported_running');
  assert.ok(diagnostic(out.code).startsWith('Tous les indices frais indiquent'));
  assert.ok(!/stub standard/.test(diagnostic(out.code)), 'the unproven "standard stub" claim is gone');
});

test('the agent journal shows the exact cause, the RAM-load state and the category, nothing else', async () => {
  const { AgentJournal } = await import('../web/agent-journal.js');
  const j = new AgentJournal();
  j.append({ stage: 'result', command: P4_TRACE, id: 'c27ec3242d', state: 'failed', code: 'diagnostic_stub_reported_running',
    detail: { phase: 'diagnostic_stub_load', category: 'refused_before_load', ram_load: 'not_started', error_type: 'IdlabDiagnosticError',
      origin_code: 'diagnostic_stub_reported_running', secret: 'TOKEN=hunter2' } });
  const line = JSON.stringify(j.rows);
  for (const needle of ['un stub tourne', 'Chargement RAM du stub', 'chargement RAM non commencé', 'refused_before_load', 'IdlabDiagnosticError', 'diagnostic.p4_usb_trace'])
    assert.ok(line.includes(needle), needle);
  assert.ok(!line.includes('hunter2'));
});

// ------------------------------------------------------------------ the REAL connection flow (the defect the first campaign exposed)
// esptool-js: main() = detectChip + runStub() (the standard stub then RUNS in the chip); detectChip() alone leaves the ROM untouched.
import { FakeTransport, ESPError } from './fakes.mjs';
function chipWorld(stubRunning = false) { return { stubRunning, mainCalls: 0, detectModes: [], runStubCalls: 0, loaders: [] }; }
function esptoolFor(world) {
  class ESPLoader {
    constructor() {
      const sim = simP4(); world.loaders.push(sim);
      sim.syncStubDetected = false; sim.chip = null;
      sim.sync = async () => { sim.syncStubDetected = world.stubRunning; };   // connection-time/recovery sync: the flag path (real esptool-js would throw on a stub)
      sim.syncAnswers = () => world.stubRunning ? [0] : new Array(8).fill(1);
      sim.detectChip = async mode => {
        world.detectModes.push(mode); await sim.sync();
        sim.chip = { CHIP_NAME: 'ESP32-P4', IMAGE_CHIP_ID: 18, USB_RAM_BLOCK: 0x800, getChipRevision: async () => 302, getChipDescription: async () => 'ESP32-P4 (revision v3.2)', readMac: async () => { if (world.macError) throw world.macError; return world.mac ?? 'aa:bb:cc:dd:ee:ff'; } };
      };
      sim.runStub = async () => { world.runStubCalls++; if (!sim.syncStubDetected) world.stubRunning = true; sim.IS_STUB = true; };
      sim.main = async mode => { world.mainCalls++; await sim.detectChip(mode); await sim.runStub(); return sim.chip.getChipDescription(sim); };
      sim.readFlashId = async () => { if (world.jedecError) throw world.jedecError; return world.jedec ?? 0x164068; };
      const finish = sim.memFinish; sim.memFinish = async e => { await finish(e); world.stubRunning = true; };   // the diagnostic stub now runs
      return sim;
    }
  }
  return { ESPLoader, Transport: FakeTransport, ESPError };
}
async function connect(world, { romOnly }) {
  const events = [], holds = [];
  const engine = new EspEngine({ getInfo: () => ({}) }, esptoolFor(world), (n, d) => events.push([n, d]), v => holds.push(v));
  Object.assign(engine, { native: true, usbSerialJtag: false, manualDownload: true, romOnly });
  await engine.identity();                       // what board-manager does when a board is added
  let closed = 0; const t = engine.transport, original = t.disconnect.bind(t); t.disconnect = async () => { closed++; return original(); };
  const context = { check() {}, live: () => true, device, download: async () => stubBytes, upload: async () => ({}), deferCompletion: true, complete() {}, report() {} };
  return { engine, context, holds, closes: () => closed };
}

test('REGRESSION of the field failure: a normal connection leaves the standard stub running, so the diagnostic is not offered and cannot start', async () => {
  const world = chipWorld(), x = await connect(world, { romOnly: false });
  assert.deepEqual([world.mainCalls, world.runStubCalls, world.stubRunning], [1, 1, true]);
  assert.ok(!x.engine.capabilities().includes(P4_TRACE));
  await assert.rejects(x.engine.execute(P4_TRACE, clean(goodArgs()), x.context), e => e.code === 'diagnostic_unavailable');
});

test('ROM-only connection: detectChip only, no main(), no runStub(), no stub uploaded; only identify/mark/diagnostic are offered', async () => {
  const world = chipWorld(), x = await connect(world, { romOnly: true });
  assert.deepEqual([world.mainCalls, world.runStubCalls, world.stubRunning, world.detectModes], [0, 0, false, ['no_reset']]);
  assert.deepEqual(x.engine.capabilities(), ['identify', 'mark', P4_TRACE, 'diagnostic.p4_usb_bringup', 'diagnostic.p4_flasher_read']);
  const loader = world.loaders[0]; assert.equal(loader.IS_STUB, false);
  const result = await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);       // T runs on THE connection, no second sync, no reset
  assert.equal(result.md5, MD5_ALL); assert.equal(world.detectModes.length, 1); assert.equal(x.closes(), 0);
  assert.equal(world.stubRunning, true); noForbidden(loader);
});

test('ROM-only: everything that needs the standard stub is refused locally, nothing is sent to the chip', async () => {
  const world = chipWorld(), x = await connect(world, { romOnly: true }), sent = world.loaders[0].calls.length;
  for (const command of ['partitions', 'flash.read', 'flash.write', 'flash.erase', 'console', 'uart.write', 'uart.configure', 'trigger', 'http.request', 'reset', 'io0', 'boot.download'])
    await assert.rejects(x.engine.execute(command, {}, x.context), e => e.code === 'diagnostic_rom_only_session', command);
  assert.equal(world.loaders[0].calls.length, sent);
});

test('a stub still running at T (the operator did not really return to ROM) is an explicit refusal, not hardware_command_failed', async () => {
  const world = chipWorld(true), x = await connect(world, { romOnly: true });        // the chip answers the sync like a stub
  let thrown; try { await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context); } catch (e) { thrown = e; }
  const out = hardwareError(thrown);
  assert.deepEqual([out.code, out.ram_load, out.category], ['diagnostic_stub_reported_running', 'not_started', 'refused_before_load']);
  assert.equal(x.engine.hold, false); assert.equal(writes(world.loaders[0]).length, 0);
});

test('FULL SEQUENCE: normal A1/D connection -> BOOT+reset -> ROM-only T -> BOOT+reset -> ROM-only recovery with proof -> normal A2 connection', async () => {
  const { Agent } = await import('../web/agent.js'), world = chipWorld();
  const normal1 = await connect(world, { romOnly: false });                 // A1 / D session: the standard stub runs
  assert.equal(world.stubRunning, true);
  world.stubRunning = false;                                                  // BOOT + reset: the chip is back in ROM
  const t = await connect(world, { romOnly: true });
  const done = await t.engine.execute(P4_TRACE, clean(goodArgs()), t.context);
  assert.equal(done.stub_resident, true); assert.equal(t.engine.diagnosticResident, true);
  await assert.rejects(t.engine.verifyRomReturn(), e => e.code === 'diagnostic_stub_still_running');   // same session: never self-recovers
  world.stubRunning = false;                                                  // BOOT + reset, port re-selected with "ROM seule"
  const r = await connect(world, { romOnly: true });
  const sent = [], agent = Object.create(Agent.prototype);
  Object.assign(agent, { engine: r.engine, active: null, awaitingResult: null, generation: 'g', log() {}, onState() {}, event: (n, d) => sent.push([n, d]) });
  await agent.message({ type: 'registered', device: 'p4', generation: 'g', quarantined: true, diagnostic_resident: true });
  assert.equal(r.engine.diagnosticResident, true);
  const loaderBefore = r.engine.loader;
  await agent.recover();
  assert.deepEqual(sent, [['recover', { rom_verified: true }]]);
  assert.equal(r.closes(), 0, 'the proof never closes the port');
  await agent.message({ type: 'recovered', generation: 'g' });
  assert.equal(r.engine.hold, false); assert.equal(r.engine.diagnosticResident, false); assert.equal(r.engine.loader, loaderBefore, 'a fresh ROM loader is kept');
  world.stubRunning = false;
  const normal2 = await connect(world, { romOnly: false });                  // A2: standard connection again
  assert.ok(normal2.engine.capabilities().includes('flash.read') && !normal2.engine.capabilities().includes(P4_TRACE));
});

test('recovery proof refused while a stub still answers: nothing is sent to the server and the hold stays', async () => {
  const { Agent } = await import('../web/agent.js'), world = chipWorld(true);
  const r = await connect(world, { romOnly: true });
  r.engine.setHold(true); r.engine.diagnosticResident = true;
  const sent = [], agent = Object.create(Agent.prototype);
  Object.assign(agent, { engine: r.engine, active: null, awaitingResult: null, generation: 'g', event: (n, d) => sent.push([n, d]) });
  await assert.rejects(agent.recover(), e => e.code === 'diagnostic_stub_still_running');
  assert.deepEqual(sent, []); assert.equal(r.engine.hold, true); assert.equal(r.engine.diagnosticResident, true); assert.equal(r.closes(), 0);
});

test('the recovery proof is a FRESH ROM sync: a stub that starts after the connection is detected', async () => {
  const world = chipWorld(), x = await connect(world, { romOnly: true });
  assert.equal(world.loaders[0].syncStubDetected, false);   // ROM at connection time
  world.stubRunning = true;                                   // something launched a stub afterwards
  await assert.rejects(x.engine.verifyRomReturn(), e => e.code === 'diagnostic_stub_still_running');
  world.stubRunning = false; assert.equal(await x.engine.verifyRomReturn(), true);
});

test('admission refusals publish the raw observations (allowlisted) and stay "not started": contradiction, communication error', async () => {
  for (const [world, code] of [[{ syncVals: [1, 0, 1, 1, 1, 1, 1, 1] }, 'diagnostic_session_state_indeterminate'], [{ failSync: true }, 'diagnostic_admission_sync_failed']]) {
    const loader = simP4(), x = await engineWith(loader);
    if (world.failSync) loader.command = async () => { throw new Error('serial lost TOKEN=hunter2'); };
    else loader.syncAnswers = () => world.syncVals;
    let thrown; try { await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context); } catch (e) { thrown = e; }
    const out = published(thrown);
    assert.deepEqual([out.code, out.phase, out.category, out.ram_load], [code, 'diagnostic_stub_load', 'refused_before_load', 'not_started']);
    assert.equal(writes(loader).length, 0); assert.equal(x.engine.diagnosticResident, false);
    assert.ok(!JSON.stringify(out).includes('hunter2'), 'loader text never crosses the protocol');
    if (!world.failSync) { assert.deepEqual(out.sync_values, world.syncVals); assert.equal(out.sync_count, 8); }
    assert.ok(diagnostic(code).length > 20);
  }
});

test('the published report accepts only well-formed raw observations', () => {
  const out = hardwareError(Object.assign(new Error('x'), { code: 'diagnostic_stub_reported_running', diagnostic: {
    sync_values: [0, -1, 0x7fffffff, 0, 0, 0, 0, 0], sync_zero_count: 6, sync_stub_detected: 1, sync_count: 8, sync_completed: 0, loader_stub: 1, md5_shape: 'stub' } }));
  assert.deepEqual([out.sync_values.length, out.sync_zero_count, out.sync_stub_detected, out.sync_count, out.sync_completed, out.md5_shape], [8, 6, 1, 8, 0, 'stub']);
  for (const bad of [{ sync_values: new Array(9).fill(0) }, { sync_values: ['0x1'] }, { sync_values: [0x80000000] }, { sync_values: [1.5] }, { sync_values: 'abc' }, { md5_shape: 'TOKEN=x' }]) {
    const o = hardwareError(Object.assign(new Error('x'), { code: 'diagnostic_stub_reported_running', diagnostic: bad }));
    assert.ok(!('sync_values' in o) && !('md5_shape' in o), JSON.stringify(bad));
  }
});

// ------------------------------------------------------------------ recovery proof = the same fresh ROM/stub detection
async function recoveryWorld(arrange) {
  const { Agent } = await import('../web/agent.js'), world = chipWorld(), r = await connect(world, { romOnly: true });
  r.engine.setHold(true); r.engine.diagnosticResident = true;
  const sim = world.loaders[0]; sim.calls.length = 0; world.detectModes.length = 0; arrange(sim, world);
  const sent = [], agent = Object.create(Agent.prototype);
  Object.assign(agent, { engine: r.engine, active: null, awaitingResult: null, generation: 'g', event: (n, d) => sent.push([n, d]) });
  const outcome = await agent.recover().then(() => null, e => e);
  const kept = r.engine.hold === true && r.engine.diagnosticResident === true && r.closes() === 0;
  return { sim, sent, outcome, kept, world };
}
const onlySyncTraffic = sim => sim.calls.every(c => ['sync', 'sync_frame', 'ack'].includes(Array.isArray(c) ? c[0] : c)) && writes(sim).length === 0;

test('recovery proof: ROM confirmed by every index is the only accepted verdict, with no reset, RAM load or NOR write', async () => {
  const x = await recoveryWorld(() => {});
  assert.equal(x.outcome, null); assert.deepEqual(x.sent, [['recover', { rom_verified: true }]]);
  assert.ok(onlySyncTraffic(x.sim)); assert.deepEqual(x.world.detectModes, [], 'no new chip detection / reset'); assert.equal(x.world.runStubCalls, 0);
});

test('recovery proof: an active stub (one zero answer, sync incomplete) is refused explicitly, quarantine kept, nothing sent', async () => {
  const x = await recoveryWorld((sim, world) => { world.stubRunning = true; sim.sync = async () => { sim.syncStubDetected = false; throw new Error('invalid response'); }; });
  assert.equal(x.outcome.code, 'diagnostic_stub_still_running');
  const out = hardwareError(x.outcome);
  assert.deepEqual([out.sync_values, out.sync_count, out.sync_completed, out.phase], [[0], 1, 0, 'control']);
  assert.deepEqual(x.sent, []); assert.ok(x.kept); assert.ok(onlySyncTraffic(x.sim));
});

test('recovery proof: contradictory answers are refused as indeterminate, never as a proof', async () => {
  for (const [name, arrange] of [
    ['mixed values', sim => { sim.syncAnswers = () => [1, 0, 1, 1, 1, 1, 1, 1]; }],
    ['partial ROM answers', sim => { sim.syncAnswers = () => [1, 1, 1]; }],
    ['heuristic says stub, exchange says ROM', sim => { const s = sim.sync; sim.sync = async () => { await s(); sim.syncStubDetected = true; }; }],
    ['heuristic says ROM, exchange says stub', (sim, world) => { sim.syncAnswers = () => [0]; }],
  ]) {
    const x = await recoveryWorld(arrange);
    assert.equal(x.outcome.code, 'diagnostic_recovery_state_indeterminate', name);
    assert.equal(hardwareError(x.outcome).phase, 'control'); assert.ok(diagnostic(x.outcome.code).length > 20);
    assert.deepEqual(x.sent, [], name); assert.ok(x.kept, name); assert.ok(onlySyncTraffic(x.sim), name);
  }
});

test('recovery proof: an interrupted communication is a structured failure, a lost transport keeps its own code', async () => {
  let x = await recoveryWorld(sim => { sim.command = async () => { throw new Error('serial lost TOKEN=hunter2'); }; });
  assert.equal(x.outcome.code, 'diagnostic_recovery_sync_failed'); assert.ok(!JSON.stringify(hardwareError(x.outcome)).includes('hunter2'));
  assert.deepEqual(x.sent, []); assert.ok(x.kept);
  x = await recoveryWorld(sim => { sim.command = async () => { throw Object.assign(new Error('closed'), { kind: 'closed' }); }; });
  assert.equal(hardwareError(x.outcome).code, 'serial_disconnected'); assert.deepEqual(x.sent, []); assert.ok(x.kept);
  x = await recoveryWorld(sim => { sim.command = async () => { throw Object.assign(new Error('x'), { name: 'NetworkError' }); }; });
  assert.equal(hardwareError(x.outcome, true).code, 'serial_disconnected'); assert.ok(x.kept);
  x = await recoveryWorld(sim => { sim.syncAnswers = () => [NaN]; });                      // an invalid value is a failure, not a zero
  assert.equal(x.outcome.code, 'diagnostic_recovery_sync_failed'); assert.ok(x.kept);
});

// ------------------------------------------------------------------ ROM seule: MCU identity verified, flash capacity measured or unknown
async function romIdentity(over = {}) {
  const world = Object.assign(chipWorld(), over), events = [];
  const engine = new EspEngine({ getInfo: () => ({}) }, esptoolFor(world), (n, d) => events.push([n, d]), () => {});
  Object.assign(engine, { native: true, usbSerialJtag: false, manualDownload: true, romOnly: true });
  const outcome = await engine.identity().then(v => ({ value: v }), e => ({ error: e }));
  return { ...outcome, world, engine, events, sim: world.loaders[0] };
}
const jedecEvent = events => events.filter(([n, d]) => n === 'transport' && /JEDEC ROM seule/.test(d.detail)).map(([, d]) => d.detail);

test('ROM seule, valid JEDEC: the capacity is measured, the identity carries family, MAC and JEDEC', async () => {
  const r = await romIdentity();
  assert.deepEqual(r.value, { chip: 'esp32p4', flash_size: 2 ** 22, flash_id: 0x164068, mac: 'aa:bb:cc:dd:ee:ff', flash_size_known: true });
  assert.match(jedecEvent(r.events)[0], /0x00164068 · lecture ok · tentative 1 · stub non/);
});

test('ROM seule, invalid JEDEC (any raw value): connection allowed, capacity declared unknown, raw id and context kept, nothing invented', async () => {
  for (const jedec of [0, 0x00ffffff, 0x010000, 0x1b0000, 0xffffffff]) {
    const r = await romIdentity({ jedec });
    assert.equal(r.error, undefined, String(jedec));
    assert.deepEqual([r.value.chip, r.value.mac, r.value.flash_size, r.value.flash_id, r.value.flash_size_known], ['esp32p4', 'aa:bb:cc:dd:ee:ff', null, null, false]);
    assert.deepEqual(r.value.flash_capacity, { jedec_id: jedec >>> 0, read: 'invalid', identify_index: 1, loader_stub: 0, rom_only: true });
    assert.match(jedecEvent(r.events)[0], /capacité flash inconnue/);
  }
});

test('ROM seule, JEDEC unavailable (the read fails): unknown capacity, context "error"; a lost transport is NOT downgraded', async () => {
  const r = await romIdentity({ jedecError: new Error('read failed') });
  assert.deepEqual([r.value.flash_size, r.value.flash_size_known, r.value.flash_capacity.read, r.value.flash_capacity.jedec_id], [null, false, 'error', null]);
  assert.match(jedecEvent(r.events)[0], /non lu · lecture error/);
  for (const lost of [Object.assign(new Error('x'), { kind: 'closed' }), Object.assign(new Error('x'), { name: 'NetworkError' })]) {
    const x = await romIdentity({ jedecError: lost });
    assert.equal(x.error, lost);
  }
});

test('ROM seule: an unverifiable MCU identity (unreadable, zero or malformed MAC) refuses the connection, whatever the JEDEC says', async () => {
  for (const over of [{ macError: new Error('efuse') }, { mac: '00:00:00:00:00:00' }, { mac: 'ff:ff:ff:ff:ff:ff' }, { mac: 'aa:bb' }, { mac: 42 }, { mac: 'aa:bb:cc:dd:ee:ff', macError: new Error('x'), jedec: 0x164068 }]) {
    const r = await romIdentity({ jedec: 0, ...over });
    assert.ok(r.error, JSON.stringify(over));
  }
  const r = await romIdentity({ mac: 'aa:bb' });
  assert.equal(r.error.code, 'physical_identity_unverified'); assert.equal(hardwareError(r.error).code, 'physical_identity_unverified');
  assert.ok(diagnostic('physical_identity_unverified').length > 20);
});

test('normal (non ROM-only) connection: an invalid JEDEC still refuses with flash_capacity_unknown and the raw id', async () => {
  const world = Object.assign(chipWorld(), { jedec: 0 });
  const engine = new EspEngine({ getInfo: () => ({}) }, esptoolFor(world), () => {}, () => {});
  Object.assign(engine, { native: true, usbSerialJtag: false, manualDownload: true, romOnly: false });
  await assert.rejects(engine.identity(), e => e.code === 'flash_capacity_unknown' && e.detail.jedec_id === 0 && e.detail.identify_index === 1);
});

test('a ROM-only identity of another family keeps the strict capacity rule', async () => {
  const world = Object.assign(chipWorld(), { jedec: 0 }), esptool = esptoolFor(world);
  const engine = new EspEngine({ getInfo: () => ({}) }, esptool, () => {}, () => {});
  Object.assign(engine, { native: true, usbSerialJtag: false, manualDownload: true, romOnly: true });
  const original = esptool.ESPLoader; esptool.ESPLoader = class extends original { constructor(o) { super(o); this.detectChip = async () => { this.chip = { CHIP_NAME: 'ESP32-S3', getChipDescription: async () => 'ESP32-S3 (revision v0.2)', readMac: async () => 'aa:bb:cc:dd:ee:ff' }; }; } };
  await assert.rejects(engine.identity(), e => e.code === 'flash_capacity_unknown');
});

test('flash operations that depend on the capacity are refused when it is unknown, before any hardware work', async () => {
  const r = await romIdentity({ jedec: 0 });
  for (const command of ['partitions', 'flash.read', 'flash.write', 'flash.erase'])
    await assert.rejects(r.engine.execute(command, {}, { check() {}, live: () => true, device: { ...device, flash_size: null }, report() {} }), e => ['diagnostic_rom_only_session', 'flash_capacity_unknown'].includes(e.code), command);
});

test('the diagnostic itself does not depend on the capacity: it runs on an unknown-capacity ROM-only session', async () => {
  const world = Object.assign(chipWorld(), { jedec: 0 }), events = [];
  const engine = new EspEngine({ getInfo: () => ({}) }, esptoolFor(world), (n, d) => events.push([n, d]), () => {});
  Object.assign(engine, { native: true, usbSerialJtag: false, manualDownload: true, romOnly: true });
  const id = await engine.identity();
  assert.equal(id.flash_size_known, false);
  assert.ok(engine.capabilities().includes(P4_TRACE));
  const context = { check() {}, live: () => true, device: { ...device, flash_size: null, flash_size_known: false }, download: async () => stubBytes, upload: async () => ({}), deferCompletion: true, complete() {}, report() {} };
  const done = await engine.execute(P4_TRACE, clean(goodArgs()), context);
  assert.equal(done.stub_resident, true);
});

// ------------------------------------------------------------------ RAM-load observability, end to end (engine -> structured error -> journal)
test('success: the operation result carries the admission observations and the per-step RAM report', async () => {
  const x = await engineWith(simP4());
  const done = await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);
  assert.deepEqual([done.admission.admission_verdict, done.admission.sync_count, done.admission.sync_zero_count, done.admission.sync_completed, done.admission.sync_stub_detected, done.admission.loader_stub], ['rom', 8, 0, 1, 0, 0]);
  assert.equal(done.admission.sync_values.length, 8);
  assert.ok(done.ram_load.steps.length >= 3 && done.ram_load.steps.every(e => e.acked === 1) && done.ram_load.failed_index === null);
  assert.deepEqual([done.ram_load.ram_block, done.ram_load.ram_block_default, done.ram_load.ram_block_source, done.ram_load.usb_otg_confirmed], [2048, 0x1800, 'usb_otg_cap', 1]);
});

test('RAM-load failure: the published error localises the failing command and keeps the last acknowledged one (allowlisted fields only)', async () => {
  for (const [fault, failedStep] of [['memBegin', 'mem_begin'], ['load_truncated', 'mem_block']]) {
    const loader = simP4({ fault }), x = await engineWith(loader);
    const returned = await x.engine.execute(P4_TRACE, clean(goodArgs()), x.context);   // a failure after the load began is RETURNED as indeterminate
    assert.deepEqual([returned.state, returned.hold], ['indeterminate', true]);
    const out = returned.error;
    assert.deepEqual([out.code, out.origin_code, out.phase, out.category, out.ram_load], ['hardware_outcome_unknown', 'diagnostic_stub_ram_load_failed', 'diagnostic_stub_load', 'ram_load_failed', 'started']);
    const failed = out.ram_steps[out.ram_failed_index];
    assert.deepEqual([failed.step, failed.started, failed.acked, failed.outcome], [failedStep, 1, 0, 'error']);
    assert.equal(out.ram_acked_count, out.ram_failed_index); assert.deepEqual([out.ram_block_size, out.ram_block_default, out.ram_block_source, out.usb_otg_confirmed], [2048, 0x1800, 'usb_otg_cap', 1]);
    if (out.ram_failed_index > 0) assert.equal(out.ram_last_acked_index, out.ram_failed_index - 1); else assert.ok(!('ram_last_acked_index' in out));
    assert.equal(out.admission_verdict, 'rom'); assert.equal(out.sync_count, 8); assert.equal(out.sync_zero_count, 0);
    assert.ok(!JSON.stringify(out).includes('lost') && !JSON.stringify(out).includes('truncated'), 'loader text never crosses the protocol');
    assert.equal(x.engine.diagnosticResident, true, 'indeterminate: the hold and the resident mark stay');
  }
});

test('the allowlist keeps well-formed RAM steps only and drops the whole list on any malformed entry', () => {
  const good = { step: 'mem_block', segment: 'text', block: 0, blocks: 2, address: 0x4ff50000, size: 64, ram_block: 64, started: 1, acked: 1, duration_ms: 5, outcome: 'ack' };
  const make = steps => hardwareError(Object.assign(new Error('x'), { code: 'diagnostic_stub_ram_load_failed', diagnostic: { ram_steps: steps, ram_acked_count: 1, ram_failed_index: 1, ram_last_acked_index: 0, ram_block_size: 64 } }));
  assert.deepEqual(make([good]).ram_steps, [good]);
  assert.deepEqual([make([good]).ram_acked_count, make([good]).ram_failed_index, make([good]).ram_last_acked_index, make([good]).ram_block_size], [1, 1, 0, 64]);
  for (const bad of [{ ...good, step: 'flash_begin' }, { ...good, segment: 'bss' }, { ...good, outcome: 'ok' }, { ...good, acked: 2 }, { ...good, acked: 0 }, { ...good, address: -1 },
      { ...good, size: 1.5 }, { ...good, duration_ms: 'x' }, { ...good, block: -1 }, { ...good, outcome: 'timeout', acked: 1 }, null, 'x'])
    assert.ok(!('ram_steps' in make([good, bad])), JSON.stringify(bad));
  assert.ok(!('ram_steps' in make(new Array(33).fill(good)))); assert.ok(!('ram_steps' in make([]))); assert.ok(!('ram_steps' in make('abc')));
  const extra = make([{ ...good, bytes: 'AAAA', note: 'TOKEN=x' }]).ram_steps[0];
  assert.ok(!('bytes' in extra) && !('note' in extra), 'unknown fields are not copied');
});

test('the agent journal shows where the RAM download stopped and the synchronisation counts', async () => {
  const { AgentJournal } = await import('../web/agent-journal.js'), j = new AgentJournal();
  j.append({ stage: 'result', command: P4_TRACE, id: '0e66794a', state: 'indeterminate', code: 'diagnostic_stub_ram_load_failed',
    detail: { phase: 'diagnostic_stub_load', category: 'ram_load_failed', ram_load: 'started', error_type: 'IdlabDiagnosticError',
      ram_acked_count: 1, sync_values: new Array(8).fill(5), sync_count: 8, sync_zero_count: 0,
      ram_steps: [{ step: 'mem_begin', segment: 'text', blocks: 2, address: 1, size: 2, ram_block: 64, started: 1, acked: 1, duration_ms: 4, outcome: 'ack' },
        { step: 'mem_block', segment: 'text', block: 0, blocks: 2, address: 1, size: 2, ram_block: 64, started: 1, acked: 0, duration_ms: 3001, outcome: 'timeout' }] } });
  const line = JSON.stringify(j.rows);
  for (const needle of ['mem_block text bloc 1/2', 'commencée, non acquittée', 'timeout', '3001 ms', '1 étape(s) acquittée(s)', '8 réponse(s), 0 à zéro'])
    assert.ok(line.includes(needle), needle);
});

// ------------------------------------------------------------------ per-packet SHA-256 in the T result
test('the T result carries 256 per-packet SHA-256 consistent with the global digest, ACKs and MD5; no flash bytes', async () => {
  const loader = simP4();
  const { result } = await runP4UsbTrace({ loader, romOnly: true, args: clean(goodArgs()), device, stubBytes });
  const h = result.packet_hashes;
  assert.equal(result.packet_hashes_algo, 'sha256_of_received_bytes'); assert.equal(h.length, 256);
  assert.deepEqual(h.map(e => e.address), [...h.keys()].map(i => 0x8000 + i * 4096));
  assert.ok(h.every((e, i) => e.size === 4096 && e.sha256.length === 64 && e.ack_index === i + 1 && e.ack_bytes === (i + 1) * 4096 && e.acked === 1));
  assert.equal(h.reduce((n, e) => n + e.size, 0), result.bytes); assert.equal(h.length, result.acks);
  const { createHash } = await import('node:crypto');
  assert.equal(createHash('sha256').update(all()).digest('hex'), result.data_sha256);
  for (const i of [0, 1, 100, 255]) assert.equal(h[i].sha256, createHash('sha256').update(all().subarray(i * 4096, (i + 1) * 4096)).digest('hex'));
  assert.ok(!JSON.stringify(result).includes(Buffer.from(all().subarray(0, 64)).toString('base64').slice(0, 40)), 'no flash bytes in the result');
  assert.ok(JSON.stringify(result).length < 200000, 'the result stays small enough for the protocol');
});
test('the packet hashes are computed AFTER the measured wall time (they never delay an ACK nor enter host_duration_ms)', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../web/p4-diagnostic.js', import.meta.url), 'utf8');
  assert.ok(src.indexOf('const wallMs = Math.round(now() - start)') < src.indexOf('await packetHashes('), 'order in runP4UsbTrace');
  const trace = readFileSync(new URL('../web/p4-stub-trace.js', import.meta.url), 'utf8');
  const loop = trace.slice(trace.indexOf('while (received < size)'), trace.indexOf('// Drain the final MD5 frame'));
  assert.ok(!/sha256|digest|packetHashes/.test(loop), 'no hashing inside the receive/ACK loop');
});

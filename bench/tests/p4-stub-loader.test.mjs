import test from 'node:test';
import assert from 'node:assert/strict';
import {P4_USB_OTG_RAM_BLOCK, MAX_REPORTED_RAM_STEPS, classifyRamLoadError, loadDiagnosticStub, retireDiagnosticStub, parseStubJson, sha256Hex, RAM_WINDOWS,
  IdlabDiagnosticError, FORBIDDEN_FLASH_METHODS} from '../web/p4-stub-loader.js';
import {IDLAB_GET_FLASH_TRACE} from '../web/p4-stub-trace.js';
import {hardwareError} from '../web/hardware-errors.js';

const b64 = bytes => Buffer.from(bytes).toString('base64');
function stubJson(over = {}) {
  const text = new Uint8Array(5000).fill(0x13), data = new Uint8Array(3000).fill(0x07);   // 3 + 2 blocks of 0x800
  return JSON.stringify({entry: 0x4ff50010, text: b64(text), text_start: 0x4ff50000,
    data: b64(data), data_start: 0x4ff60040, bss_start: 0x4ff60000, ...over});
}
function report() {
  const b = new Uint8Array(64), v = new DataView(b.buffer);
  v.setUint32(0, 0x31544449, true); v.setUint32(4, 1, true);
  return b;
}

// A loader that records every call, so that a test can assert what was (not) done.
// syncVals: the eight raw `val` of the fresh ESP_SYNC exchange; syncDetected: esptool's own flag after loader.sync().
function makeLoader({chip = 'ESP32-P4', revision = 302, isStub = false, greeting = 'OHAI', probe = 'ok',
    syncVals = null, syncDetected = null, syncError = null, memFault = null, otg = true, chipBlock = 0x800} = {}) {
  const seen = {memBegin: 0, memBlock: 0, memFinish: 0};
  // memFault: {call, nth, error}: the nth (0-based) call of `call` throws `error` (a timeout, a closed port, ...)
  const maybeFail = call => { const n = seen[call]++; if (memFault && memFault.call === call && memFault.nth === n) throw memFault.error; };
  const calls = [];
  const loader = {
    IS_STUB: isStub, ESP_RAM_BLOCK: 0x1800, DEFAULT_TIMEOUT: 1000, calls,
    async usesUsbOtg() { calls.push('usesUsbOtg'); if (otg instanceof Error) throw otg; return otg; },   // esptool-js 0.7.0: VID/PID (+ UARTDEV_BUF_NO fallback)
    // esptool-js 0.7.0 semantics: sync() throws on a stub; command(0x08) returns the first answer, readPacket the next ones (then throws)
    async sync() {
      calls.push('sync'); if (syncError === 'sync') throw new Error('no sync');
      loader.syncStubDetected = syncDetected ?? isStub; if (isStub && syncDetected === null) throw new Error('invalid response');
    },
    async command(op) {
      calls.push('sync_frame'); if (syncError === 'command') throw new Error('serial lost');
      loader.pending = [...(syncVals ?? (isStub ? [0] : ROM))]; return [loader.pending.shift(), new Uint8Array(0)];
    },
    async readPacket() { if (!loader.pending.length) throw new Error('no data'); return [loader.pending.shift(), new Uint8Array(0)]; },
    pending: [],
    chip: {CHIP_NAME: chip, USB_RAM_BLOCK: chipBlock, async getChipRevision() { calls.push('getChipRevision'); return revision; }},
    async memBegin(...a) { calls.push(['memBegin', ...a]); maybeFail('memBegin'); },
    async memBlock(bytes, seq) { calls.push(['memBlock', bytes.length, seq]); maybeFail('memBlock'); },
    async memFinish(entry) { calls.push(['memFinish', entry]); maybeFail('memFinish'); },
    async checkCommand(_label, op) {
      calls.push(['checkCommand', op]);
      if (probe !== 'ok') throw new Error('stock stub rejects the opcode');
      return report();
    },
    transport: {async read() { calls.push('read'); return Uint8Array.from(greeting, c => c.charCodeAt(0)); }},
  };
  for (const name of FORBIDDEN_FLASH_METHODS)
    loader[name] = async () => { calls.push(['FORBIDDEN', name]); throw new Error('flash method called'); };
  return loader;
}
const ROM = [1, 1, 1, 1, 1, 1, 1, 1], ZEROS = [0];
async function options(json, over = {}) {
  return {consent: true, romOnly: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json)), ...over};
}
const chipWrites = loader => loader.calls.filter(c => Array.isArray(c) && /^mem/.test(c[0]));

test('without consent, a SHA-256 or with another SHA-256 nothing is sent to the chip', async () => {
  const json = stubJson(), good = await options(json);
  for (const [opts, code] of [[{...good, consent: false}, 'consent_missing'], [{...good, consent: 'yes'}, 'consent_missing'],
      [{consent: true}, 'sha256_missing'], [{consent: true, expectedSha256: 'xyz'}, 'sha256_missing'],
      [{...good, expectedSha256: '0'.repeat(64)}, 'sha256_mismatch']]) {
    const loader = makeLoader();
    await assert.rejects(loadDiagnosticStub(loader, json, opts), e => e instanceof IdlabDiagnosticError && e.code.endsWith(code));
    assert.equal(chipWrites(loader).length, 0);
    assert.equal(loader.IS_STUB, false);
  }
});

test('a wrong chip, an old revision or an already running stub is refused before any write', async () => {
  const json = stubJson(), opts = await options(json);
  for (const [loaderOptions, code] of [[{chip: 'ESP32-S3'}, 'wrong_chip'], [{revision: 200}, 'unsupported_chip_revision'],
      [{revision: 299}, 'unsupported_chip_revision'], [{isStub: true}, 'stub_reported_running']]) {
    const loader = makeLoader(loaderOptions);
    await assert.rejects(loadDiagnosticStub(loader, json, opts), e => e.code.endsWith(code) && e.sessionIndeterminate === false);
    assert.equal(chipWrites(loader).length, 0);
  }
});

test('the ECO5+ boundary: revision 3.00 is accepted, 3.02 (the bench board) too', async () => {
  const json = stubJson(), opts = await options(json);
  for (const revision of [300, 302]) {
    const result = await loadDiagnosticStub(makeLoader({revision}), json, opts);
    assert.equal(result.chip_revision, revision);
  }
});

test('an invalid or out-of-RAM stub JSON is refused before any write', async () => {
  const cases = {
    stub_json_not_json: '{nope', stub_json_unexpected_keys: JSON.stringify({entry: 1}),
    stub_json_bad_address: stubJson({entry: -4}),
    stub_text_outside_iram: stubJson({text_start: RAM_WINDOWS.text.start + RAM_WINDOWS.text.length - 50}),
    stub_data_outside_dram: stubJson({data_start: RAM_WINDOWS.data.start + RAM_WINDOWS.data.length - 8}),
    stub_bss_outside_dram: stubJson({bss_start: 0x40000000}),
    stub_entry_outside_text: stubJson({entry: 0x4ff50000 + 5000}),
    stub_json_bad_base64: stubJson({text: '***not base64***'}),
  };
  for (const [code, json] of Object.entries(cases)) {
    const loader = makeLoader();
    const opts = {consent: true, romOnly: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json))};
    await assert.rejects(loadDiagnosticStub(loader, json, opts), e => e.code === code, code);
    assert.equal(chipWrites(loader).length, 0, code);
  }
  assert.throws(() => parseStubJson(stubJson({extra: 1})), /unexpected_keys/);
});

test('the normal path loads exactly two RAM segments, starts the entry, reads OHAI, identifies and never touches the flash', async () => {
  const json = stubJson(), loader = makeLoader();
  const result = await loadDiagnosticStub(loader, json, await options(json));
  assert.deepEqual(loader.calls.filter(c => Array.isArray(c) && c[0] === 'memBegin'),
    [['memBegin', 5000, 3, 2048, 0x4ff50000], ['memBegin', 3000, 2, 2048, 0x4ff60040]]);
  assert.deepEqual(loader.calls.filter(c => Array.isArray(c) && c[0] === 'memBlock').map(c => c.slice(1)), [[2048, 0], [2048, 1], [904, 2], [2048, 0], [952, 1]]);
  assert.deepEqual(loader.calls.filter(c => Array.isArray(c) && c[0] === 'memFinish'), [['memFinish', 0x4ff50010]]);
  assert.equal(loader.calls.filter(c => Array.isArray(c) && c[0] === 'FORBIDDEN').length, 0);
  assert.deepEqual(loader.calls.filter(c => Array.isArray(c) && c[0] === 'checkCommand'), [['checkCommand', IDLAB_GET_FLASH_TRACE]]);
  assert.equal(loader.__idlabTraceStub, true);
  assert.equal(loader.__idlabTraceStubSha256, await sha256Hex(new TextEncoder().encode(json)));
  assert.equal(result.ram_only, true);
  // the order matters: identity checks first, then RAM writes, then the greeting, then the probe
  const order = loader.calls.map(c => Array.isArray(c) ? c[0] : c);
  assert.ok(order.indexOf('getChipRevision') < order.indexOf('memBegin') && order.indexOf('memFinish') < order.indexOf('read')
    && order.indexOf('read') < order.indexOf('checkCommand'));
});

test('a wrong greeting leaves the session undetermined and the stub unidentified', async () => {
  const json = stubJson(), loader = makeLoader({greeting: 'NOPE'});
  await assert.rejects(loadDiagnosticStub(loader, json, await options(json)),
    e => e.code === 'diagnostic_stub_bad_greeting' && e.sessionIndeterminate === true && e.noRetry === true);
  assert.notEqual(loader.__idlabTraceStub, true);
});

test('a stock stub that rejects the probe is never marked as the diagnostic stub', async () => {
  const json = stubJson(), loader = makeLoader({probe: 'reject'});
  await assert.rejects(loadDiagnosticStub(loader, json, await options(json)),
    e => e.code === 'diagnostic_stub_not_identified' && e.sessionIndeterminate === true);
  assert.equal(loader.__idlabTraceStub, false);
  assert.equal(loader.__idlabTraceStubSha256, undefined);
});

test('a failure while writing RAM is reported as undetermined and is never retried', async () => {
  const json = stubJson(), loader = makeLoader();
  let writes = 0;
  loader.memBlock = async () => { writes++; throw new Error('usb reset'); };
  await assert.rejects(loadDiagnosticStub(loader, json, await options(json)),
    e => e.code === 'diagnostic_stub_ram_load_failed' && e.sessionIndeterminate === true && e.noRetry === true);
  assert.equal(writes, 1);
});

test('a missing greeting (timeout) is reported as undetermined', async () => {
  const json = stubJson(), loader = makeLoader();
  loader.transport.read = async () => { throw new Error('timeout'); };
  await assert.rejects(loadDiagnosticStub(loader, json, await options(json)),
    e => e.code === 'diagnostic_stub_no_greeting' && e.sessionIndeterminate === true);
});

test('retiring the diagnostic stub clears the browser state, asks for the operator reset and does nothing else', async () => {
  const json = stubJson(), loader = makeLoader();
  await loadDiagnosticStub(loader, json, await options(json));
  const before = loader.calls.length;
  assert.deepEqual(retireDiagnosticStub(loader), {requires_operator_reset: true});
  assert.equal(loader.__idlabTraceStub, false);
  assert.equal(loader.IS_STUB, false);
  assert.equal(loader.calls.length, before);
});

// ---------------------------------------------------------------- admission: ROM / stub detection (fresh, multi-index)
async function admit(loaderOptions, extra = {}) {
  const json = stubJson(), loader = makeLoader(loaderOptions);
  let error = null, result = null;
  try { result = await loadDiagnosticStub(loader, json, {...await options(json), ...extra}); } catch (e) { error = e; }
  return {loader, error, result};
}

test('ROM confirmed by every index: the sync is redone BEFORE the first RAM write and the load proceeds', async () => {
  const {loader, error, result} = await admit({});
  assert.equal(error, null); assert.equal(result.ram_only, true);
  const firstWrite = loader.calls.findIndex(c => Array.isArray(c) && c[0] === 'memBegin');
  assert.ok(loader.calls.indexOf('sync') >= 0 && loader.calls.indexOf('sync') < firstWrite);
  assert.ok(loader.calls.indexOf('sync_frame') < firstWrite);
});

test('stub confirmed (a stub answers once with 0 and sync() throws): explicit refusal with the raw values, nothing written', async () => {
  const {loader, error} = await admit({isStub: true});
  assert.equal(error.code, 'diagnostic_stub_reported_running'); assert.equal(error.sessionIndeterminate, false);
  assert.deepEqual(error.sessionState, {sync_values: [0], sync_count: 1, sync_zero_count: 1, sync_completed: 0, sync_stub_detected: 1, loader_stub: 1, admission_verdict: 'stub'});
  assert.equal(chipWrites(loader).length, 0);
});

test('a stub left by an earlier page (IS_STUB false here) is refused, never loaded over', async () => {
  const {loader, error} = await admit({isStub: false, syncVals: ZEROS, syncDetected: true});
  assert.equal(error.code, 'diagnostic_stub_reported_running'); assert.equal(chipWrites(loader).length, 0);
});

test('contradictory or incomplete indices are an indeterminate state (never "stub", never "ROM")', async () => {
  for (const [name, opts] of [
    ['esptool says stub, the exchange looks like a ROM', {syncVals: ROM, syncDetected: true}],
    ['esptool says ROM, the exchange is a single zero', {syncVals: ZEROS, syncDetected: false}],
    ['mixed raw values (flapping session)', {syncVals: [1, 0, 1, 1, 0, 1, 1, 1], syncDetected: false}],
    ['one zero among eight', {syncVals: [1, 1, 1, 1, 1, 1, 1, 0], syncDetected: false}],
    ['only three answers, all non-zero', {syncVals: [1, 1, 1], syncDetected: false}],
    ['a single non-zero answer', {syncVals: [1], syncDetected: false}],
    ['the page claims a stub but the fresh sync looks like a ROM', {isStub: true, syncVals: ROM, syncDetected: false}],
  ]) {
    const {loader, error} = await admit(opts);
    assert.equal(error.code, 'diagnostic_session_state_indeterminate', name);
    assert.equal(error.sessionIndeterminate, false, name); assert.equal(chipWrites(loader).length, 0, name);
  }
});

test('an invalid raw value aborts the admission: it is never turned into a zero (nor into a ROM)', async () => {
  for (const bad of [undefined, null, NaN, '1', 1.5, 2 ** 31, -(2 ** 31) - 1, Infinity]) {
    for (const vals of [[bad], [1, 1, 1, 1, 1, 1, 1, bad]]) {
      const {loader, error} = await admit({syncVals: vals});
      assert.equal(error.code, 'diagnostic_admission_sync_failed', String(bad)); assert.equal(chipWrites(loader).length, 0);
    }
  }
});

test('syncStubDetected is not an absolute proof: it cannot clear a stub nor condemn a ROM alone', async () => {
  assert.equal((await admit({syncVals: ROM, syncDetected: true})).error.code, 'diagnostic_session_state_indeterminate');
  assert.equal((await admit({syncVals: ZEROS, syncDetected: false})).error.code, 'diagnostic_session_state_indeterminate');
});

test('a communication error during the admission is its own refusal: nothing written, state unobservable, no retry', async () => {
  const {loader, error} = await admit({syncError: 'command'});
  assert.equal(error.code, 'diagnostic_admission_sync_failed');
  assert.equal(error.sessionIndeterminate, false); assert.equal(chipWrites(loader).length, 0);
  assert.equal(loader.calls.filter(c => c === 'sync_frame').length, 1, 'no retry');
  // sync() failing alone is what a stub does: the raw exchange decides (a ROM-looking exchange then contradicts it)
  assert.equal((await admit({syncError: 'sync'})).error.code, 'diagnostic_session_state_indeterminate');
});

test('the optional independent MD5 probe: agreeing passes; disagreeing or "unknown" never gives a positive ROM verdict', async () => {
  const ascii = Uint8Array.from('0123456789abcdef0123456789abcdef', c => c.charCodeAt(0)), raw = new Uint8Array(16);
  assert.equal((await admit({}, {md5Probe: async () => ascii})).error, null);
  const stubAnswer = await admit({}, {md5Probe: async () => raw});
  assert.equal(stubAnswer.error.code, 'diagnostic_session_state_indeterminate'); assert.equal(stubAnswer.error.sessionState.md5_shape, 'stub');
  for (const unknown of [new Uint8Array(7), new Uint8Array(32).fill(0xff), null]) {
    const r = await admit({}, {md5Probe: async () => unknown});
    assert.equal(r.error.code, 'diagnostic_session_state_indeterminate'); assert.equal(r.error.sessionState.md5_shape, 'unknown'); assert.equal(chipWrites(r.loader).length, 0);
  }
  assert.equal((await admit({isStub: true}, {md5Probe: async () => ascii})).error.code, 'diagnostic_session_state_indeterminate');
  const plain = await admit({});
  assert.ok(!plain.loader.calls.some(c => Array.isArray(c) && c[0] === 'checkCommand' && c[1] !== IDLAB_GET_FLASH_TRACE), 'no md5 probe by default');
});

// ---------------------------------------------------------------- RAM download observability (observation only)
const timeoutError = () => Object.assign(new Error('No serial data received.'), {kind: 'no-response'});
let tick = 0;
const clock = () => { tick += 7; return tick; };           // deterministic: every command lasts exactly 7 ms (one reading before, one after)
async function load(loaderOptions) {
  const json = stubJson(), loader = makeLoader(loaderOptions);
  let error = null, result = null; tick = 0;
  try { result = await loadDiagnosticStub(loader, json, {...await options(json), now: clock}); } catch (e) { error = e; }
  return {loader, error, result, json};
}
const step = (name, segment, extra) => ({step: name, segment, ram_block: 2048, started: 1, ...extra});

test('success: the admission observations and one acknowledged entry per RAM command are returned with the load', async () => {
  const {result} = await load({});
  assert.deepEqual(result.admission, {sync_values: ROM, sync_count: 8, sync_zero_count: 0, sync_completed: 1, sync_stub_detected: 0, loader_stub: 0, admission_verdict: 'rom'});
  const r = result.ram_load;
  assert.deepEqual([r.ram_block, r.ram_block_default, r.ram_block_source, r.usb_otg_confirmed], [2048, 0x1800, 'usb_otg_cap', 1]);
  assert.deepEqual([r.acked_count, r.failed_index, r.last_acked_index], [8, null, 7]);
  assert.deepEqual(r.steps.map(e => [e.step, e.segment, e.block ?? null, e.blocks ?? null, e.address, e.size, e.acked, e.outcome, e.duration_ms]), [
    ['mem_begin', 'text', null, 3, 0x4ff50000, 5000, 1, 'ack', 7], ['mem_block', 'text', 0, 3, 0x4ff50000, 2048, 1, 'ack', 7],
    ['mem_block', 'text', 1, 3, 0x4ff50800, 2048, 1, 'ack', 7], ['mem_block', 'text', 2, 3, 0x4ff51000, 904, 1, 'ack', 7],
    ['mem_begin', 'data', null, 2, 0x4ff60040, 3000, 1, 'ack', 7], ['mem_block', 'data', 0, 2, 0x4ff60040, 2048, 1, 'ack', 7],
    ['mem_block', 'data', 1, 2, 0x4ff60840, 952, 1, 'ack', 7], ['mem_finish', 'entry', null, null, 0x4ff50010, 0, 1, 'ack', 7]]);
  assert.ok(r.steps.every(e => e.started === 1 && e.ram_block === 2048));
});

test('the instrumentation changes nothing on the chip: same commands, same arguments, same order, no retry', async () => {
  const {loader} = await load({});
  const commands = loader.calls.filter(c => Array.isArray(c) && /^mem/.test(c[0]));
  assert.deepEqual(commands, [['memBegin', 5000, 3, 2048, 0x4ff50000], ['memBlock', 2048, 0], ['memBlock', 2048, 1], ['memBlock', 904, 2],
    ['memBegin', 3000, 2, 2048, 0x4ff60040], ['memBlock', 2048, 0], ['memBlock', 952, 1], ['memFinish', 0x4ff50010]]);
  const failed = await load({memFault: {call: 'memBlock', nth: 1, error: timeoutError()}});
  assert.equal(failed.loader.calls.filter(c => Array.isArray(c) && /^mem/.test(c[0])).length, 3, 'nothing is sent after the failure');
});

const failures = [
  ['timeout on the first memBegin', {call: 'memBegin', nth: 0}, 0, null, [step('mem_begin', 'text', {blocks: 3, address: 0x4ff50000, size: 5000, acked: 0, outcome: 'timeout', duration_ms: 7})]],
  ['timeout on an intermediate memBlock', {call: 'memBlock', nth: 1}, 2, 1, null],
  ['failure on the second memBegin', {call: 'memBegin', nth: 1}, 4, 3, null],
  ['failure on memFinish', {call: 'memFinish', nth: 0}, 7, 6, null],
];
for (const [name, fault, failedIndex, lastAcked, exactSteps] of failures) {
  test(`RAM load: ${name} — localised, last acknowledged command kept, session indeterminate, admission kept`, async () => {
    const {error} = await load({memFault: {...fault, error: timeoutError()}});
    assert.equal(error.code, 'diagnostic_stub_ram_load_failed'); assert.equal(error.sessionIndeterminate, true);
    const r = error.ramLoad;
    assert.equal(r.failed_index, failedIndex); assert.equal(r.last_acked_index, lastAcked);
    assert.equal(r.acked_count, failedIndex); assert.equal(r.steps.length, failedIndex + 1, 'nothing after the failing command');
    const failedStep = r.steps[failedIndex];
    assert.deepEqual([failedStep.started, failedStep.acked, failedStep.outcome, failedStep.duration_ms], [1, 0, 'timeout', 7]);
    assert.ok(r.steps.slice(0, failedIndex).every(e => e.acked === 1 && e.outcome === 'ack'));
    if (exactSteps) assert.deepEqual(r.steps, exactSteps);
    assert.equal(error.sessionState.admission_verdict, 'rom'); assert.equal(error.sessionState.sync_count, 8);
  });
}

test('error classes are derived locally from fixed signals, never from free text; the loader message is not kept', async () => {
  const cases = [[timeoutError(), 'timeout'], [Object.assign(new Error('No serial data received.'), {}), 'timeout'], [new Error('Serial data stream stopped: Possible serial noise or corruption.'), 'timeout'],
    [Object.assign(new Error('x'), {kind: 'closed'}), 'closed'], [Object.assign(new Error('x'), {name: 'NetworkError'}), 'closed'],
    [Object.assign(new Error('x'), {kind: 'partial-packet'}), 'invalid'], [Object.assign(new Error('x'), {kind: 'invalid-packet'}), 'invalid'],
    [new Error('Failed to enter RAM download mode TOKEN=hunter2'), 'error']];
  for (const [err, outcome] of cases) {
    const {error} = await load({memFault: {call: 'memBegin', nth: 0, error: err}});
    assert.equal(error.ramLoad.steps[0].outcome, outcome, err.message);
    assert.ok(!JSON.stringify(error.ramLoad).includes('hunter2') && !JSON.stringify(error.ramLoad).includes('Failed to'));
  }
  assert.equal(classifyRamLoadError(undefined), 'error');
});

test('the report holds typed numbers and fixed words only: no stub bytes, no free text', async () => {
  const {result} = await load({});
  const allowed = new Set(['step', 'segment', 'block', 'blocks', 'address', 'size', 'ram_block', 'started', 'acked', 'duration_ms', 'outcome']);
  for (const e of result.ram_load.steps) {
    assert.ok(Object.keys(e).every(k => allowed.has(k)), Object.keys(e).join());
    assert.ok(Object.values(e).every(v => Number.isInteger(v) || ['mem_begin', 'mem_block', 'mem_finish', 'text', 'data', 'entry', 'ack'].includes(v)));
  }
  assert.ok(!JSON.stringify(result.ram_load).includes(Buffer.from(new Uint8Array(64).fill(0x13)).toString('base64').slice(0, 40)));
});

test('greeting / identification failures after the RAM load keep the complete (acknowledged) RAM report and the admission', async () => {
  const bad = await load({greeting: 'NOPE'});
  assert.equal(bad.error.code, 'diagnostic_stub_bad_greeting');
  assert.deepEqual([bad.error.ramLoad.acked_count, bad.error.ramLoad.failed_index, bad.error.ramLoad.last_acked_index], [8, null, 7]);
  assert.equal(bad.error.sessionState.admission_verdict, 'rom');
  const silent = await load({probe: 'reject'});
  assert.equal(silent.error.code, 'diagnostic_stub_not_identified'); assert.equal(silent.error.ramLoad.acked_count, 8);
});

test('refusals before the RAM load carry no RAM report (nothing started)', async () => {
  const {error} = await load({isStub: true});
  assert.equal(error.ramLoad, undefined); assert.equal(error.sessionState.admission_verdict, 'stub');
});

// ---------------------------------------------------------------- USB-OTG RAM block (0x800): conditions, refusals, no extra effect
test('ESP32-P4, ROM-only session, USB-OTG positively identified: ESP_RAM_BLOCK becomes 0x800 BEFORE the first memBegin, nothing else changes', async () => {
  const {loader, result, error} = await load({});
  assert.equal(error, null); assert.equal(loader.ESP_RAM_BLOCK, 0x800); assert.equal(P4_USB_OTG_RAM_BLOCK, 0x800);
  const order = loader.calls.map(c => Array.isArray(c) ? c[0] : c);
  assert.ok(order.indexOf('usesUsbOtg') > order.indexOf('sync_frame') && order.indexOf('usesUsbOtg') < order.indexOf('memBegin'));
  assert.deepEqual([result.ram_load.ram_block, result.ram_load.ram_block_default, result.ram_load.ram_block_source], [0x800, 0x1800, 'usb_otg_cap']);
});

test('transport not positively identified as USB-OTG: refused before any RAM transfer, structured, block size untouched', async () => {
  for (const otg of [false, null, 0, 'yes', new Error('no VID/PID and register read failed')]) {
    const {loader, error} = await load({otg});
    assert.equal(error.code, 'diagnostic_transport_unconfirmed', String(otg)); assert.equal(error.sessionIndeterminate, false);
    assert.equal(chipWrites(loader).length, 0); assert.equal(loader.ESP_RAM_BLOCK, 0x1800, 'never assumed');
    assert.equal(error.sessionState.admission_verdict, 'rom'); assert.equal(error.ramLoad, undefined);
    assert.ok(!loader.calls.includes('FORBIDDEN'));
  }
  const noMethod = makeLoader({}); delete noMethod.usesUsbOtg; const json = stubJson();
  await assert.rejects(loadDiagnosticStub(noMethod, json, await options(json)), e => e.code === 'diagnostic_transport_unconfirmed');
  assert.equal(chipWrites(noMethod).length, 0);
});

test('a lost transport during the OTG check keeps its own error (not "unconfirmed")', async () => {
  for (const lost of [Object.assign(new Error('x'), {kind: 'closed'}), Object.assign(new Error('x'), {name: 'NetworkError'})]) {
    const {error, loader} = await load({otg: lost});
    assert.equal(error, lost); assert.equal(chipWrites(loader).length, 0);
  }
});

test('the library USB_RAM_BLOCK must be present and exactly 0x800: absent, null, mistyped or different values are refused, never defaulted', async () => {
  for (const chipBlock of [0x1000, 0x1800, 0x400, 0, null, '0x800', '2048', 2048.5, NaN, [0x800], {}]) {
    const {loader, error} = await load({chipBlock});
    assert.equal(error.code, 'diagnostic_ram_block_unexpected', String(chipBlock)); assert.equal(error.sessionIndeterminate, false);
    assert.equal(chipWrites(loader).length, 0); assert.equal(loader.ESP_RAM_BLOCK, 0x1800, 'block size untouched'); assert.equal(error.ramLoad, undefined);
    assert.equal(error.sessionState.admission_verdict, 'rom');
  }
  // regression: an ABSENT value (the property does not exist on the chip class) is refused too
  const json = stubJson(), loader = makeLoader({}); delete loader.chip.USB_RAM_BLOCK;
  await assert.rejects(loadDiagnosticStub(loader, json, await options(json)), e => e.code === 'diagnostic_ram_block_unexpected');
  assert.equal(chipWrites(loader).length, 0); assert.equal(loader.ESP_RAM_BLOCK, 0x1800);
  const noChipValue = makeLoader({}); noChipValue.chip = {CHIP_NAME: 'ESP32-P4', async getChipRevision() { return 302; }};
  await assert.rejects(loadDiagnosticStub(noChipValue, json, await options(json)), e => e.code === 'diagnostic_ram_block_unexpected');
  assert.equal(chipWrites(noChipValue).length, 0);
});

test('the adaptation exists for the ROM-only session only: any other call is refused before any I/O', async () => {
  for (const romOnly of [false, undefined, 'true', 1]) {
    const json = stubJson(), loader = makeLoader();
    await assert.rejects(loadDiagnosticStub(loader, json, {...await options(json), romOnly}), e => e.code === 'diagnostic_rom_only_required');
    assert.deepEqual(loader.calls, [], 'no frame at all'); assert.equal(loader.ESP_RAM_BLOCK, 0x1800);
  }
});

test('no RAM operation may already have started on this loader (no second attempt, no retry)', async () => {
  const first = await load({memFault: {call: 'memBlock', nth: 0, error: timeoutError()}});
  assert.equal(first.error.code, 'diagnostic_stub_ram_load_failed');
  const json = stubJson(), again = first.loader, before = again.calls.length;
  await assert.rejects(loadDiagnosticStub(again, json, await options(json)), e => e.code === 'diagnostic_ram_load_already_started');
  assert.equal(again.calls.length, before, 'nothing sent');
  assert.equal(again.ESP_RAM_BLOCK, 0x800);
});

test('the only write of ESP_RAM_BLOCK in the web code is the guarded adaptation (other MCUs and transports are untouched)', async () => {
  const {readdirSync, readFileSync} = await import('node:fs');
  const dir = new URL('../web/', import.meta.url), hits = [];
  for (const f of readdirSync(dir).filter(f => f.endsWith('.js'))) {
    readFileSync(new URL(f, dir), 'utf8').split('\n').forEach((line, i) => { if (/ESP_RAM_BLOCK\s*=[^=]/.test(line)) hits.push(`${f}:${i + 1}`); });
  }
  assert.equal(hits.length, 1); assert.match(hits[0], /^p4-stub-loader\.js:/);
  for (const method of ['postConnect', 'powerOnFlash', 'disableWatchdogs', 'runStub', 'main', 'flashSpiAttach', 'hardReset', 'writeReg', 'detectChip', 'after', 'changeBaud'])
    for (const f of ['p4-stub-loader.js', 'p4-session-state.js', 'p4-diagnostic.js'])
      assert.ok(!new RegExp(`\\.${method}\\s*\\(`).test(readFileSync(new URL(f, dir), 'utf8').replace(/\/\/.*$/gm, '')), `${f} must not call .${method}()`);
});

test('another chip is refused before any frame, as before', async () => {
  const json = stubJson(), loader = makeLoader({chip: 'ESP32-S3'});
  await assert.rejects(loadDiagnosticStub(loader, json, await options(json)), e => e.code === 'diagnostic_stub_wrong_chip');
  assert.deepEqual(loader.calls, []); assert.equal(loader.ESP_RAM_BLOCK, 0x1800);
});

// ---------------------------------------------------------------- bounded step list (never lets the whole trace disappear)
const bigDataJson = () => stubJson({data: b64(new Uint8Array(90000).fill(7))});     // many blocks whatever the block size
async function loadBig(loaderOptions) {
  const json = bigDataJson(), loader = makeLoader(loaderOptions);
  let error = null, result = null; tick = 0;
  try { result = await loadDiagnosticStub(loader, json, {...await options(json), now: clock}); } catch (e) { error = e; }
  return {loader, error, result};
}

test('a long RAM load keeps at most 32 entries: the first ones, the last acknowledged command and the failing one; the omitted count is exact', async () => {
  const {error} = await loadBig({memFault: {call: 'memBlock', nth: 40, error: timeoutError()}});
  const r = error.ramLoad;
  assert.ok(r.failed_index > MAX_REPORTED_RAM_STEPS, 'the failure happens beyond the bound');
  assert.equal(r.steps.length, MAX_REPORTED_RAM_STEPS);
  assert.equal(r.steps_omitted, r.failed_index + 1 - MAX_REPORTED_RAM_STEPS);
  const [lastAcked, failed] = r.steps.slice(-2);
  assert.deepEqual([failed.acked, failed.outcome, lastAcked.acked, lastAcked.outcome], [0, 'timeout', 1, 'ack']);
  assert.deepEqual([r.acked_count, r.failed_index, r.last_acked_index], [r.failed_index, r.failed_index, r.failed_index - 1]);
  assert.equal(r.steps[0].step, 'mem_begin');
  // what crosses the protocol keeps the trace (the list is accepted as a whole) and the omitted count
  const out = hardwareError(Object.assign(new Error('x'), {code: 'diagnostic_stub_ram_load_failed', diagnostic: {ram_steps: r.steps, ram_steps_omitted: r.steps_omitted, ram_failed_index: r.failed_index, ram_acked_count: r.acked_count}}));
  assert.equal(out.ram_steps.length, MAX_REPORTED_RAM_STEPS); assert.equal(out.ram_steps_omitted, r.steps_omitted);
  assert.deepEqual(out.ram_steps.slice(-2), r.steps.slice(-2));
});

test('a short RAM load is untouched by the bound (the qualified stub needs 9 commands with 0x800 blocks)', async () => {
  const {result} = await load({});
  assert.ok(result.ram_load.steps.length <= MAX_REPORTED_RAM_STEPS); assert.equal(result.ram_load.steps_omitted, 0);
  // text 8952 B + data 276 B (qualified stub): 2 begins + ceil(8952/2048)=5 + ceil(276/2048)=1 blocks + 1 finish
  const count = block => 2 + Math.ceil(8952 / block) + Math.ceil(276 / block) + 1;
  assert.deepEqual([count(0x800), count(0x1800)], [9, 6]);
});

test('a successful long load reports its first entries and its final two commands', async () => {
  const {result} = await loadBig({});
  const r = result.ram_load;
  assert.equal(r.steps.length, MAX_REPORTED_RAM_STEPS); assert.ok(r.steps_omitted > 0);
  assert.deepEqual(r.steps.slice(-1)[0].step, 'mem_finish'); assert.equal(r.failed_index, null);
  assert.equal(r.acked_count, r.steps_omitted + MAX_REPORTED_RAM_STEPS);
});

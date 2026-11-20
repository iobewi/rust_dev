import test from 'node:test';
import assert from 'node:assert/strict';
import {loadDiagnosticStub, retireDiagnosticStub, parseStubJson, sha256Hex, RAM_WINDOWS,
  IdlabDiagnosticError, FORBIDDEN_FLASH_METHODS} from '../web/p4-stub-loader.js';
import {IDLAB_GET_FLASH_TRACE} from '../web/p4-stub-trace.js';

const b64 = bytes => Buffer.from(bytes).toString('base64');
function stubJson(over = {}) {
  const text = new Uint8Array(100).fill(0x13), data = new Uint8Array(40).fill(0x07);
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
    syncVals = null, syncDetected = null, syncError = null} = {}) {
  const calls = [];
  const loader = {
    IS_STUB: isStub, ESP_RAM_BLOCK: 64, DEFAULT_TIMEOUT: 1000, calls,
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
    chip: {CHIP_NAME: chip, async getChipRevision() { calls.push('getChipRevision'); return revision; }},
    async memBegin(...a) { calls.push(['memBegin', ...a]); },
    async memBlock(bytes, seq) { calls.push(['memBlock', bytes.length, seq]); },
    async memFinish(entry) { calls.push(['memFinish', entry]); },
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
  return {consent: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json)), ...over};
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
    stub_entry_outside_text: stubJson({entry: 0x4ff50000 + 100}),
    stub_json_bad_base64: stubJson({text: '***not base64***'}),
  };
  for (const [code, json] of Object.entries(cases)) {
    const loader = makeLoader();
    const opts = {consent: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json))};
    await assert.rejects(loadDiagnosticStub(loader, json, opts), e => e.code === code, code);
    assert.equal(chipWrites(loader).length, 0, code);
  }
  assert.throws(() => parseStubJson(stubJson({extra: 1})), /unexpected_keys/);
});

test('the normal path loads exactly two RAM segments, starts the entry, reads OHAI, identifies and never touches the flash', async () => {
  const json = stubJson(), loader = makeLoader();
  const result = await loadDiagnosticStub(loader, json, await options(json));
  assert.deepEqual(loader.calls.filter(c => Array.isArray(c) && c[0] === 'memBegin'),
    [['memBegin', 100, 2, 64, 0x4ff50000], ['memBegin', 40, 1, 64, 0x4ff60040]]);
  assert.deepEqual(loader.calls.filter(c => Array.isArray(c) && c[0] === 'memBlock').map(c => c.slice(1)), [[64, 0], [36, 1], [40, 0]]);
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
  assert.deepEqual(error.sessionState, {sync_values: [0], sync_count: 1, sync_zero_count: 1, sync_completed: 0, sync_stub_detected: 1, loader_stub: 1});
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

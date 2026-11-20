// Off-hardware integration: the REAL esptool-js 0.7.0 ESPLoader (command/readPacket/sync) driven by a scripted transport.
// No Web Serial, no port, no hardware. Pinned by package.json devDependencies ("esptool-js": "0.7.0").
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {ESPLoader, ESPError} from 'esptool-js';
import {observeSessionState, judgeSessionState, SYNC_OPCODE} from '../web/p4-session-state.js';
import {loadDiagnosticStub, sha256Hex} from '../web/p4-stub-loader.js';

const pinned = JSON.parse(readFileSync(new URL('../node_modules/esptool-js/package.json', import.meta.url))).version;

// Scripted chip behind a transport with the esptool-js Transport surface used by ESPLoader (write / read / flushInput).
// `chip` decides how many answers a sync gets and with which `val` (ROM: 8 non-zero; stub: 1 zero).
function scriptedTransport(answersFor) {
  const queue = [], writes = [];
  const packet = (op, val) => { const p = new Uint8Array(10); p[0] = 1; p[1] = op; p[4] = val & 255; p[5] = val >> 8 & 255; p[6] = val >> 16 & 255; p[7] = val >> 24 & 255; return p; };
  const transport = {tracing: false, writes, queue, getInfo: () => 'scripted transport (no Web Serial)',
    flushInput() { queue.length = 0; },
    async drainInput() { queue.length = 0; },
    async write(pkt) { writes.push(Uint8Array.from(pkt)); for (const val of answersFor(pkt[1])) queue.push(packet(pkt[1], val)); },
    async read() { if (!queue.length) throw new ESPError('No serial data received.'); return queue.shift(); }};
  return transport;
}
const loaderOn = transport => new ESPLoader({transport, baudrate: 115200, terminal: {clean() {}, write() {}, writeLine() {}}});
const ROM_CHIP = op => op === 0x08 ? new Array(8).fill(0x0000ab12) : [0], STUB_CHIP = op => op === 0x08 ? [0] : [0];

test('the pinned package really is esptool-js 0.7.0 and ESPLoader has no ESP_SYNC property (the opcode is the literal 0x08)', () => {
  assert.equal(pinned, '0.7.0');
  const loader = loaderOn(scriptedTransport(ROM_CHIP));
  assert.equal(loader.ESP_SYNC, undefined);
  assert.equal(typeof loader.command, 'function'); assert.equal(typeof loader.readPacket, 'function'); assert.equal(typeof loader.sync, 'function');
});

test('real loader against a ROM: sync() completes, the exchange gives 8 raw values, no answer is left behind, verdict ROM', async () => {
  const transport = scriptedTransport(ROM_CHIP), loader = loaderOn(transport);
  const e = await observeSessionState(loader);
  assert.equal(e.syncCompleted, true); assert.equal(e.syncStubDetected, false);
  assert.deepEqual(e.syncValues, new Array(8).fill(0x0000ab12));
  assert.equal(transport.queue.length, 0, 'no residual response');
  const sent = transport.writes.map(p => [p[0], p[1], p.length]);
  assert.deepEqual(sent, [[0, SYNC_OPCODE, 44], [0, SYNC_OPCODE, 44]], 'sync() frame + our exchange frame, nothing else is transmitted');
  assert.equal(judgeSessionState(e).verdict, 'rom');
});

test('real loader against a stub: sync() throws (one answer), our exchange keeps the single zero, verdict stub, no residual', async () => {
  const transport = scriptedTransport(STUB_CHIP), loader = loaderOn(transport);
  const e = await observeSessionState(loader);
  assert.deepEqual([e.syncCompleted, e.syncValues], [false, [0]]);
  assert.equal(transport.queue.length, 0);
  assert.equal(judgeSessionState(e).verdict, 'stub');
});

test('real loader, 0x8000_0000-range val: esptool-js decodes a signed int32, kept as is (never coerced)', async () => {
  const loader = loaderOn(scriptedTransport(op => op === 0x08 ? new Array(8).fill(0x80000001 | 0) : [0]));
  const e = await observeSessionState(loader);
  assert.deepEqual(e.syncValues, new Array(8).fill(-0x7fffffff));
});

test('real loader, silent line: the first answer missing is a communication failure, not a zero and not a stub', async () => {
  const loader = loaderOn(scriptedTransport(() => []));
  await assert.rejects(observeSessionState(loader), ESPError);
});

test('real loader, partial ROM answers (3): kept as 3 values, verdict indeterminate', async () => {
  const loader = loaderOn(scriptedTransport(op => op === 0x08 ? [5, 5, 5] : [0]));
  const e = await observeSessionState(loader);
  assert.deepEqual([e.syncCompleted, e.syncValues.length], [false, 3]);
  assert.equal(judgeSessionState(e).verdict, 'indeterminate');
});

test('admission on the real loader: a stub is refused and a ROM-looking session reaches the RAM load only through the checks', async () => {
  const json = JSON.stringify({entry: 0x4ff50010, text: Buffer.from(new Uint8Array(8)).toString('base64'), text_start: 0x4ff50000,
    data: Buffer.from(new Uint8Array(8)).toString('base64'), data_start: 0x4ff60040, bss_start: 0x4ff60000});
  const opts = {consent: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json))};
  const stubTransport = scriptedTransport(STUB_CHIP), stub = loaderOn(stubTransport);
  stub.chip = {CHIP_NAME: 'ESP32-P4', async getChipRevision() { throw new Error('must not be reached'); }};
  await assert.rejects(loadDiagnosticStub(stub, json, opts), e => e.code === 'diagnostic_stub_reported_running' && e.sessionIndeterminate === false);
  assert.ok(stubTransport.writes.every(p => p[1] === SYNC_OPCODE), 'only sync frames were transmitted');
  const romTransport = scriptedTransport(ROM_CHIP), rom = loaderOn(romTransport);
  rom.chip = {CHIP_NAME: 'ESP32-P4', async getChipRevision() { return 200; }};
  await assert.rejects(loadDiagnosticStub(rom, json, opts), e => e.code === 'diagnostic_stub_unsupported_chip_revision');   // admitted as ROM, then stopped by the revision check
  assert.ok(romTransport.writes.every(p => p[1] === SYNC_OPCODE), 'no memory command was transmitted');
});

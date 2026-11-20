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
function scriptedTransport(answersFor, usb = {vid: 0x303a, pid: 0x12}) {
  const queue = [], writes = [];
  const packet = (op, val) => { const p = new Uint8Array(10); p[0] = 1; p[1] = op; p[4] = val & 255; p[5] = val >> 8 & 255; p[6] = val >> 16 & 255; p[7] = val >> 24 & 255; return p; };
  const transport = {tracing: false, writes, queue, getInfo: () => 'scripted transport (no Web Serial)', getVid: () => usb.vid, getPid: () => usb.pid,
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
  const opts = {consent: true, romOnly: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json))};
  const stubTransport = scriptedTransport(STUB_CHIP), stub = loaderOn(stubTransport);
  stub.chip = {CHIP_NAME: 'ESP32-P4', async getChipRevision() { throw new Error('must not be reached'); }};
  await assert.rejects(loadDiagnosticStub(stub, json, opts), e => e.code === 'diagnostic_stub_reported_running' && e.sessionIndeterminate === false);
  assert.ok(stubTransport.writes.every(p => p[1] === SYNC_OPCODE), 'only sync frames were transmitted');
  const romTransport = scriptedTransport(ROM_CHIP), rom = loaderOn(romTransport);
  rom.chip = {CHIP_NAME: 'ESP32-P4', async getChipRevision() { return 200; }};
  await assert.rejects(loadDiagnosticStub(rom, json, opts), e => e.code === 'diagnostic_stub_unsupported_chip_revision');   // admitted as ROM, then stopped by the revision check
  assert.ok(romTransport.writes.every(p => p[1] === SYNC_OPCODE), 'no memory command was transmitted');
});

// ---------------------------------------------------------------- RAM download with the REAL esptool-js 0.7.0 (scripted transport, no Web Serial)
const b64 = bytes => Buffer.from(bytes).toString('base64');
const bigStub = () => JSON.stringify({entry: 0x4ff50010, text: b64(new Uint8Array(5000).fill(0x13)), text_start: 0x4ff50000,
  data: b64(new Uint8Array(3000).fill(7)), data_start: 0x4ff60040, bss_start: 0x4ff60000});
const memOps = {0x05: 'mem_begin', 0x07: 'mem_block', 0x06: 'mem_finish'};
const dataPacket = (op, data) => { const p = new Uint8Array(8 + data.length + 2); p[0] = 1; p[1] = op; p.set(data, 8); return p; };   // header + data + 2 status bytes (0 0)
const report = () => { const b = new Uint8Array(64), v = new DataView(b.buffer); v.setUint32(0, 0x31544449, true); v.setUint32(4, 1, true); return b; };

// A ROM that answers like the real one for the commands the diagnostic uses; `silentAt` = nth RAM command left unanswered.
function romWorld({silentAt = null, usb = {vid: 0x303a, pid: 0x12}, greeting = true} = {}) {
  let nth = 0;
  const transport = scriptedTransport(op => op === 0x08 ? new Array(8).fill(0x0000ab12) : op === 0xe0 ? [] : memOps[op] ? (nth++ === silentAt ? [] : [0]) : [0], usb);
  const originalWrite = transport.write;
  transport.write = async pkt => {
    await originalWrite(pkt);
    if (pkt[1] === 0x06 && greeting && silentAt !== 7) transport.queue.push(Uint8Array.from('OHAI', c => c.charCodeAt(0)));   // the stub greets after MEM_END
  };
  const loader = loaderOn(transport);
  const forbidden = [];
  for (const m of ['main', 'runStub', 'hardReset', 'after', 'changeBaud', 'flashSpiAttach', 'writeReg', 'detectChip', 'connect', 'flashBegin', 'flashBlock', 'flashFinish', 'eraseFlash', 'writeFlash', 'readFlash'])
    if (typeof loader[m] === 'function') loader[m] = async () => { forbidden.push(m); throw new Error('forbidden ' + m); };
  loader.chip = {CHIP_NAME: 'ESP32-P4', IMAGE_CHIP_ID: 18, USB_RAM_BLOCK: 0x800, async getChipRevision() { return 302; }};
  for (const m of ['postConnect', 'powerOnFlash', 'disableWatchdogs', 'usesUsbOtg', 'watchdogReset'])
    if (m !== 'usesUsbOtg') loader.chip[m] = async () => { forbidden.push('chip.' + m); throw new Error('forbidden chip.' + m); };
  loader.DEFAULT_TIMEOUT = 50;
  const signals = [];
  for (const m of ['setDTR', 'setRTS', 'setSignals', 'disconnect', 'connect', 'waitForUnlock']) transport[m] = async () => { signals.push(m); };
  return {transport, loader, forbidden, signals};
}
const wire = transport => transport.writes.map(p => ({op: p[1], len: p[2] | p[3] << 8, data: p.slice(8)}));

test('real loader, USB-OTG ROM: the RAM download is cut in 0x800 blocks on the wire, in the exact memBegin/memBlock/memFinish sequence', async () => {
  const w = romWorld(), json = bigStub();
  w.loader.ESP_RAM_BLOCK = 0x1800;                       // the esptool-js default, as in a ROM-only session
  const opts = {consent: true, romOnly: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json))};
  // the probe answer (IDT1 report) after the greeting
  const base = w.transport.write; w.transport.write = async pkt => { await base(pkt); if (pkt[1] === 0xe0) w.transport.queue.push(dataPacket(0xe0, report())); };
  let outcome = null; try { await loadDiagnosticStub(w.loader, json, opts); } catch (e) { outcome = e; }
  assert.equal(w.loader.ESP_RAM_BLOCK, 0x800);
  const mem = wire(w.transport).filter(c => memOps[c.op]);
  assert.deepEqual(mem.map(c => memOps[c.op]), ['mem_begin', 'mem_block', 'mem_block', 'mem_block', 'mem_begin', 'mem_block', 'mem_block', 'mem_finish']);
  const u32 = (d, i) => new DataView(d.buffer, d.byteOffset).getUint32(i * 4, true);
  assert.deepEqual([u32(mem[0].data, 0), u32(mem[0].data, 1), u32(mem[0].data, 2), u32(mem[0].data, 3)], [5000, 3, 0x800, 0x4ff50000]);   // size, blocks, blocksize, offset
  assert.deepEqual([u32(mem[4].data, 0), u32(mem[4].data, 1), u32(mem[4].data, 2), u32(mem[4].data, 3)], [3000, 2, 0x800, 0x4ff60040]);
  assert.deepEqual([1, 2, 3, 5, 6].map(i => u32(mem[i].data, 0)), [0x800, 0x800, 904, 0x800, 952]);                                      // block lengths
  assert.deepEqual([1, 2, 3, 5, 6].map(i => u32(mem[i].data, 1)), [0, 1, 2, 0, 1]);                                                       // sequence numbers
  assert.equal(u32(mem[7].data, 1), 0x4ff50010);
  assert.equal(outcome, null, 'greeting and IDT1 identification answered: the whole load succeeds on the real loader');
});

test('real loader: ROM-only path = sync frames, RAM commands only. No reset, no standard stub, no register write, no flash command, no powerOnFlash/disableWatchdogs', async () => {
  const w = romWorld({silentAt: 0}), json = bigStub();                                  // stop at the first memBegin: no need for the stub to answer
  const opts = {consent: true, romOnly: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json))};
  await assert.rejects(loadDiagnosticStub(w.loader, json, opts), e => e.code === 'diagnostic_stub_ram_load_failed');
  const ops = [...new Set(wire(w.transport).map(c => c.op))].sort((a, b) => a - b);
  assert.deepEqual(ops, [0x05, 0x08], 'only ESP_SYNC (0x08) and MEM_BEGIN (0x05)');
  assert.deepEqual(w.forbidden, [], 'no main/runStub/reset/flash/register-write/postConnect/powerOnFlash/disableWatchdogs');
  assert.deepEqual(w.signals, [], 'no DTR/RTS/disconnect/reconnect on the transport');
  const full = romWorld({}); const base = full.transport.write;
  full.transport.write = async pkt => { await base(pkt); if (pkt[1] === 0xe0) full.transport.queue.push(dataPacket(0xe0, report())); };
  await loadDiagnosticStub(full.loader, json, opts).catch(() => {});
  const all = new Set(wire(full.transport).map(c => c.op));
  for (const op of all) assert.ok([0x05, 0x06, 0x07, 0x08, 0xe0].includes(op) || op === 0x0a, `unexpected opcode 0x${op.toString(16)}`);
  assert.deepEqual(full.forbidden, []); assert.deepEqual(full.signals, []);
});

test('real loader, transport not positively USB-OTG (JTAG PID, foreign VID, no VID/PID without register helper): refused before any RAM command', async () => {
  const json = bigStub(), opts = {consent: true, romOnly: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json))};
  for (const usb of [{vid: 0x303a, pid: 0x1001}, {vid: 0x10c4, pid: 0xea60}, {vid: undefined, pid: undefined}]) {
    const w = romWorld({usb});
    await assert.rejects(loadDiagnosticStub(w.loader, json, opts), e => e.code === 'diagnostic_transport_unconfirmed' && e.sessionState.admission_verdict === 'rom', JSON.stringify(usb));
    assert.equal(w.loader.ESP_RAM_BLOCK, 0x1800, 'never assumed');
    assert.ok(wire(w.transport).every(c => c.op === 0x08), 'sync frames only');
  }
});

test('real loader: a ROM-only P4 whose VID/PID is hidden is accepted only through the chip UARTDEV_BUF_NO register check (positive evidence)', async () => {
  const json = bigStub(), opts = {consent: true, romOnly: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json))};
  for (const [answer, code] of [[true, 'diagnostic_stub_ram_load_failed'], [false, 'diagnostic_transport_unconfirmed']]) {
    const w = romWorld({usb: {vid: undefined, pid: undefined}, silentAt: 0});
    w.loader.chip.usesUsbOtg = async () => answer;
    await assert.rejects(loadDiagnosticStub(w.loader, json, opts), e => e.code === code, String(answer));
  }
});

test('real loader, RAM download traced step by step; a silent line on one command is a localised timeout (real ESPError) with the effective block size', async () => {
  const json = bigStub(), opts = {consent: true, romOnly: true, expectedSha256: await sha256Hex(new TextEncoder().encode(json))};
  for (const [silentAt, failed] of [[0, 0], [2, 2], [4, 4], [6, 6]]) {
    const w = romWorld({silentAt});
    let error = null; try { await loadDiagnosticStub(w.loader, json, opts); } catch (e) { error = e; }
    assert.equal(error.code, 'diagnostic_stub_ram_load_failed'); assert.equal(error.sessionIndeterminate, true);
    const r = error.ramLoad;
    assert.deepEqual([r.ram_block, r.ram_block_default, r.ram_block_source, r.usb_otg_confirmed, r.failed_index, r.acked_count, r.steps[failed].outcome, r.steps[failed].acked],
      [0x800, 0x1800, 'usb_otg_cap', 1, failed, failed, 'timeout', 0]);
    assert.equal(wire(w.transport).filter(c => memOps[c.op]).length, failed + 1, 'the failing command was sent once, nothing after, no retry');
  }
});

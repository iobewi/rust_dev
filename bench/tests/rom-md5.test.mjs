// ROM (no stub) mode of flash.md5: the engine opens the loader through its REAL openLoader(); only esptool-js and the serial port are doubles.
// The double ESPLoader.main() calls this.runStub() like esptool-js 0.7.0 does (detect, postConnect, runStub, baud change, flash id), so that replacing runStub on the
// instance is what keeps the stub out. It proves the engine's decisions, not the behaviour of the real ROM or of esptool-js.
import test from 'node:test';
import assert from 'node:assert/strict';
import { EspEngine } from '../web/esp-engine.js';
import { hardwareError } from '../web/hardware-errors.js';
import { ESPError, FakeTransport } from './fakes.mjs';

const MiB = 1024 * 1024;
const P4_JEDEC = 0x1940C8;
const ASCII = (hex) => Array.from(hex, c => c.charCodeAt(0).toString(16).padStart(2, '0')).join('');
const MD5 = '0123456789abcdef0123456789abcdef';

function setup({ jedec = [P4_JEDEC], syncStub = false, attachFails = false, md5 = null } = {}) {
  const log = { constructed: [], main: 0, runStub: 0, attach: [], flashId: 0, md5: 0, order: [] };
  class FakeLoader {
    constructor(options) {
      log.constructed.push({ baudrate: options.baudrate });
      this.transport = options.transport; this.IS_STUB = false; this.syncStubDetected = syncStub; this.ESP_SPI_ATTACH = 13;
      this.chip = { readMac: async () => 'aa:bb:cc:dd:ee:ff' };
    }
    // esptool-js 0.7.0 main(): ..., runStub, [changeBaud if the baud differs], readFlashId (its own, uncounted by the engine)
    async main() {
      log.main++; await this.runStub();
      log.order.push('main-flash-id');
      return 'ESP32-P4 (revision v3.2)';
    }
    async runStub() {                                  // the REAL one: uploads the stub through memory-load commands, then IS_STUB = true
      log.runStub++; this.IS_STUB = true;
      this.transport._init(); for (const [op, n] of [[5, 1], [6, 1], [7, 3]]) this.transport.commandCounts[op] = n;
      return this.chip;
    }
    async checkCommand(name, op, data) { log.order.push('attach'); log.attach.push({ name, op, length: data.length }); if (attachFails) throw new ESPError('attach failed'); return 0; }
    async readFlashId() { return jedec[Math.min(log.flashId++, jedec.length - 1)]; }
    async flashMd5sum() { log.md5++; return md5 ?? (this.IS_STUB ? MD5 : ASCII(MD5)); }
  }
  const port = { getInfo: () => ({ usbVendorId: 0x1a86 }), open: async () => {}, close: async () => {}, setSignals: async () => {}, readable: null, writable: null };
  const events = [];
  const engine = new EspEngine(port, { ESPLoader: FakeLoader, Transport: FakeTransport, ESPError }, (kind, data) => events.push([kind, data]));
  const context = { check() {}, live: () => true, checkLive() {}, device: { chip: 'esp32p4', flash_size: 32 * MiB }, report() {}, upload: async d => ({ size: d.length }) };
  return { engine, log, context, events };
}
const md5 = (x, args = {}) => x.engine.execute('flash.md5', { address: 0x2000, size: 4096, return_console: false, ...args }, x.context);

test('default flash.md5 is unchanged: the stub is uploaded, 16-byte digest, loader_stub 1', async () => {
  const x = setup();
  const r = await md5(x);
  assert.equal(r.ram_load_ops, 5, 'the stub upload is visible in the wire counters (1 begin + 1 end + 3 data)');
  assert.equal(x.log.runStub, 1); assert.equal(r.format, 'stub-raw16'); assert.equal(r.loader, 'stub'); assert.equal(r.identify.loader_stub, 1);
  assert.equal(x.log.attach.length, 0, 'no explicit SPI_ATTACH in stub mode');
});

test('loader "rom": the real runStub is never called, the ROM answer (32 ASCII) is decoded and the result says loader_stub 0, no memory-load command sent', async () => {
  const x = setup();
  const r = await md5(x, { loader: 'rom' });
  assert.equal(x.log.main, 1); assert.equal(x.log.runStub, 0, 'the stub is not uploaded');
  assert.equal(r.ram_load_ops, 0, 'no ESP_MEM_BEGIN/END/DATA written');
  assert.deepEqual({ md5: r.md5, format: r.format, loader: r.loader, stub: r.identify.loader_stub }, { md5: MD5, format: 'rom-ascii32', loader: 'rom', stub: 0 });
  assert.equal(r.identify.identify_index, 1);
});

test('ROM mode asks for the ROM baud rate (115200; esptool-js then makes no baud change) and attaches the flash with the 8-byte esptool payload', async () => {
  const x = setup();
  await md5(x, { loader: 'rom' });
  assert.deepEqual(x.log.constructed, [{ baudrate: 115200 }]);
  assert.deepEqual(x.log.attach, [{ name: 'configure SPI flash pins', op: 13, length: 8 }]);
});

test('the stub mode keeps its own baud rate and is not touched by the ROM option', async () => {
  const x = setup();
  await md5(x);
  assert.deepEqual(x.log.constructed, [{ baudrate: 460800 }]);
});

test('a stub already running on the chip is refused in ROM mode before any command', async () => {
  const x = setup({ syncStub: true });
  await assert.rejects(md5(x, { loader: 'rom' }), e => { const d = hardwareError(e); return d.code === 'rom_mode_stub_running'; });
  assert.equal(x.log.md5, 0); assert.equal(x.log.attach.length, 0);
  assert.equal(x.engine.loader, null, 'the session is closed');
});

test('a failed ROM attach is a distinct error and no measurement is made', async () => {
  const x = setup({ attachFails: true });
  await assert.rejects(md5(x, { loader: 'rom' }), e => hardwareError(e).code === 'rom_flash_attach_failed');
  assert.equal(x.log.md5, 0);
});

test('the identity check stays strict in ROM mode: read again for every command, its rank counts, a bad id is refused with the raw value', async () => {
  const x = setup({ jedec: [P4_JEDEC, P4_JEDEC, 0xFF7F0000] });
  const a = await md5(x, { loader: 'rom' }), b = await md5(x, { loader: 'rom' });
  assert.deepEqual([a.identify.identify_index, b.identify.identify_index], [1, 2]);
  assert.equal(x.log.attach.length, 1, 'one attach per loader session');
  await assert.rejects(md5(x, { loader: 'rom' }), e => { const d = hardwareError(e); return d.code === 'flash_capacity_unknown' && d.jedec_id === 0xFF7F0000 && d.identify_index === 3 && d.loader_stub === 0; });
  assert.equal(x.log.md5, 2, 'no measurement after the failed identification, no retry');
});

test('a session open in one mode is never reused for the other', async () => {
  const x = setup();
  await md5(x, { loader: 'rom' });
  await assert.rejects(md5(x), e => hardwareError(e).code === 'loader_mode_mismatch');
  const y = setup();
  await md5(y);
  await assert.rejects(md5(y, { loader: 'rom' }), e => hardwareError(e).code === 'loader_mode_mismatch');
  assert.equal(y.log.md5, 1);
});

test('a malformed ROM answer is md5_response_invalid, never a default MD5', async () => {
  for (const bad of ['', 'zz'.repeat(32), ASCII(MD5.toUpperCase()), '31'.repeat(16)]) {
    const x = setup({ md5: bad });
    await assert.rejects(md5(x, { loader: 'rom' }), e => hardwareError(e).code === 'md5_response_invalid', bad.slice(0, 12));
  }
});

test('with return_console the ROM session ends and the next ROM command opens a new session (rank 1 again)', async () => {
  const x = setup();
  x.engine.openConsole = async function () { this.loader = null; this.loaderMode = null; this.mode = 'console'; };   // the real one resets the chip; here only the state change
  const a = await md5(x, { loader: 'rom', return_console: true }), b = await md5(x, { loader: 'rom' });
  assert.deepEqual([a.identify.identify_index, b.identify.identify_index], [1, 1]);
  assert.equal(x.log.attach.length, 2);
});

test('ROM mode attaches the flash BEFORE the flash id read of esptool-js main() (esptool order: power on, attach, then flash id), and attaches once per session', async () => {
  const x = setup();
  await md5(x, { loader: 'rom' });
  assert.deepEqual(x.log.order, ['attach', 'main-flash-id']);
});

test('the attach happens inside the replaced runStub only: a stub detected at sync is refused BEFORE the attach and before any flash id read', async () => {
  const x = setup({ syncStub: true });
  await assert.rejects(md5(x, { loader: 'rom' }), e => hardwareError(e).code === 'rom_mode_stub_running');
  assert.deepEqual(x.log.order, [], 'nothing was sent to the chip after the detection');
});

// --- what the bench transport counts: command frames actually written, by opcode ------------------------------------------------
import { makeBenchTransport } from '../web/bench-transport.js';
class WritableFake extends FakeTransport { slipWriter(data) { return data; } }
function transportWith(writeImpl) {
  const { BenchTransport } = makeBenchTransport(WritableFake, ESPError);
  let end; const device = { pumpDone: new Promise((r) => { end = r; }), writable: { getWriter: () => ({ write: writeImpl, releaseLock() {} }) } };
  return new BenchTransport(device);
}
const frame = (op, length = 8, direction = 0) => { const f = new Uint8Array(length); f[0] = direction; f[1] = op; return f; };

test('the transport counts command frames written, by opcode; acknowledgements, responses and failed writes are not counted', async () => {
  const t = transportWith(async () => {});
  await t.write(frame(5)); await t.write(frame(7, 20)); await t.write(frame(7, 20)); await t.write(frame(0x13, 24));
  await t.write(Uint8Array.from([0, 16, 0, 0]));       // 4-byte cumulative acknowledgement of a flash read
  await t.write(frame(5, 8, 1));                        // direction byte 1: not a command
  assert.deepEqual(t.commandCounts, { 5: 1, 7: 2, 19: 1 });
  const bad = transportWith(async () => { throw Object.assign(new Error('x'), { name: 'NetworkError' }); });
  await assert.rejects(bad.write(frame(5)));
  assert.deepEqual(bad.commandCounts, {}, 'a write that failed was not sent');
});

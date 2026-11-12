// Experimental stub variant: the engine's REAL openLoader()/prepareVariant()/uploadVariant() with esptool-js and the serial port replaced by doubles.
// main() of the double calls this.runStub() like esptool-js 0.7.0 (detect, runStub, readFlashId), so that replacing runStub on the instance is what is tested.
// It proves the engine's decisions and the upload sequence; it does not prove anything about the real chip, the stub or esptool-js.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { EspEngine } from '../web/esp-engine.js';
import { STUB_VARIANTS } from '../web/stub-variants.js';
import { hardwareError } from '../web/hardware-errors.js';
import { ESPError, FakeTransport, feed } from './fakes.mjs';

const MiB = 1024 * 1024, P4_JEDEC = 0x1940C8, NAME = 'p4-no-clock-boost', ENTRY = STUB_VARIANTS[NAME];
const FILE = readFileSync(new URL('../web/stub-esp32p4-noboost.json', import.meta.url));
const MD5 = '0123456789abcdef0123456789abcdef';

function setup({ chip = 'ESP32-P4', revision = 302, syncStub = false, greeting = 'OHAI', deviceChip = 'esp32p4', file = FILE, fetchFails = false } = {}) {
  const log = { constructed: [], fetched: [], runStub: 0, mem: [], finish: [], order: [], md5: 0, closed: 0 };
  class FakeLoader {
    constructor(options) {
      log.constructed.push({ baudrate: options.baudrate });
      this.transport = options.transport; this.IS_STUB = false; this.syncStubDetected = syncStub; this.ESP_SPI_ATTACH = 13; this.ESP_RAM_BLOCK = 0x1800; this.DEFAULT_TIMEOUT = 60;
      this.chip = { CHIP_NAME: chip, getChipRevision: async () => revision, readMac: async () => 'aa:bb:cc:dd:ee:ff' };
    }
    async main() { await this.runStub(); log.order.push('main-flash-id'); return `${chip} (revision v${Math.floor(revision / 100)}.${revision % 100})`; }
    async runStub() { log.runStub++; this.IS_STUB = true; return this.chip; }              // the OFFICIAL one (the bundled stub)
    async memBegin(size, blocks, blockSize, address) { log.order.push('memBegin'); log.mem.push({ size, blocks, blockSize, address, blocksSent: [] }); }
    async memBlock(data, sequence) { log.mem.at(-1).blocksSent.push([data.length, sequence]); }
    async memFinish(entry) { log.order.push('memFinish'); log.finish.push(entry); if (greeting) feed(this.transport, FakeTransport.frame(Uint8Array.from(greeting, c => c.charCodeAt(0)))); }   // what the stub sends once running
    async applyUsbFlashWriteSize() { log.order.push('applyUsb'); }
    async checkCommand() { return 0; }   // the ROM SPI_ATTACH of the rom mode
    async readFlashId() { return P4_JEDEC; }
    async flashMd5sum() { log.md5++; return this.IS_STUB ? MD5 : Array.from(MD5, c => c.charCodeAt(0).toString(16)).join(''); }
  }
  const port = { getInfo: () => ({ usbVendorId: 0x1a86 }), open: async () => {}, close: async () => {}, setSignals: async () => {}, readable: null, writable: null };
  const deps = { ESPLoader: FakeLoader, Transport: FakeTransport, ESPError,
    fetchAsset: async (name) => { log.fetched.push(name); if (fetchFails) throw new Error('offline'); return file; } };
  const engine = new EspEngine(port, deps, () => {});
  const context = { check() {}, live: () => true, checkLive() {}, device: { chip: deviceChip, flash_size: 32 * MiB }, report() {}, upload: async d => ({ size: d.length }) };
  return { engine, log, context };
}
const args = (extra = {}) => ({ address: 0x2000, size: 4096, return_console: false, stub_variant: NAME, stub_sha256: ENTRY.sha256, ...extra });
const md5 = (x, a = args(), command = 'flash.md5') => x.engine.execute(command, a, x.context);
const code = (e) => hardwareError(e).code;
const untouched = (x) => { assert.deepEqual(x.log.constructed, [], 'no loader was created: the port was not touched'); assert.equal(x.log.mem.length, 0); };

test('the registry pins the file the repository ships (SHA-256 of the bytes and of the text field) and only lets reads through', async () => {
  const { createHash } = await import('node:crypto');
  assert.equal(createHash('sha256').update(FILE).digest('hex'), ENTRY.sha256);
  const stub = JSON.parse(FILE.toString());
  assert.equal(createHash('sha256').update(stub.text).digest('hex'), ENTRY.text_sha256);
  assert.deepEqual([...ENTRY.allowed_commands], ['flash.md5', 'flash.read']);
  assert.equal(ENTRY.uart_baud, 115200);
  assert.ok(Object.isFrozen(STUB_VARIANTS) && Object.isFrozen(ENTRY) && Object.isFrozen(ENTRY.allowed_commands));
});

test('the official stub stays the default: the real runStub is used, nothing is fetched, the baud rate is unchanged, the identity says so', async () => {
  const x = setup();
  const r = await md5(x, { address: 0x2000, size: 4096, return_console: false });
  assert.equal(x.log.runStub, 1); assert.deepEqual(x.log.fetched, []); assert.deepEqual(x.log.constructed, [{ baudrate: 460800 }]);
  assert.equal(r.stub_identity.variant, 'official'); assert.equal(r.stub_identity.verified, false); assert.equal(r.format, 'stub-raw16');
});

test('a variant request with the pinned SHA-256 uploads the variant (not the official stub) at 115200 baud, in esptool order, and says so in the result', async () => {
  const x = setup();
  const r = await md5(x);
  assert.equal(x.log.runStub, 0, 'the official runStub is not called'); assert.deepEqual(x.log.fetched, [ENTRY.file]);
  assert.deepEqual(x.log.constructed, [{ baudrate: 115200 }]);
  const stub = JSON.parse(FILE.toString());
  assert.deepEqual(x.log.mem.map(m => [m.size, m.address]), [[8176, stub.text_start], [204, stub.data_start]]);
  assert.ok(x.log.mem.every(m => m.blocksSent.length === m.blocks && m.blocksSent.every(([, n], i) => n === i)));
  assert.equal(x.log.mem[0].blocksSent.reduce((n, [len]) => n + len, 0), 8176);
  assert.deepEqual(x.log.finish, [stub.entry]);
  assert.deepEqual(x.log.order.slice(0, 5), ['memBegin', 'memBegin', 'memFinish', 'applyUsb', 'main-flash-id']);
  assert.equal(r.stub_identity.variant, NAME); assert.equal(r.stub_identity.verified, true);
  assert.deepEqual([r.stub_identity.file_sha256, r.stub_identity.text_sha256, r.stub_identity.uart_baud], [ENTRY.sha256, ENTRY.text_sha256, 115200]);
  assert.equal(r.stub_identity.stub_commit, ENTRY.origin.stub_commit); assert.equal(r.identify.loader_stub, 1); assert.equal(r.loader, 'stub');
});

test('flash.read with a variant carries the same identity', async () => {
  const x = setup();
  x.engine.readFlash = async (_l, _a, size) => new Uint8Array(size);
  const r = await md5(x, args(), 'flash.read');
  assert.equal(r.stub_identity.variant, NAME); assert.equal(r.artifact.size, 4096);
});

test('a request without the pinned SHA-256, or with another one, is refused before anything is fetched or the port touched', async () => {
  for (const bad of [{ stub_sha256: '0'.repeat(64) }, { stub_sha256: ENTRY.sha256.toUpperCase() }, { stub_sha256: undefined }, { stub_sha256: 5 }]) {
    const x = setup();
    await assert.rejects(md5(x, args(bad)), e => code(e) === 'stub_variant_sha_mismatch', JSON.stringify(bad));
    assert.deepEqual(x.log.fetched, []); untouched(x);
  }
});

test('a file that does not hash to the pin is refused before the port is touched (any altered byte, truncated, empty)', async () => {
  const altered = Buffer.from(FILE); altered[altered.length - 5] ^= 1;
  for (const file of [altered, FILE.subarray(0, 100), Buffer.alloc(0)]) {
    const x = setup({ file });
    await assert.rejects(md5(x), e => code(e) === 'stub_variant_sha_mismatch');
    assert.deepEqual(x.log.fetched, [ENTRY.file]); untouched(x);
  }
});

test('an unreachable file is an error, nothing is loaded', async () => {
  const x = setup({ fetchFails: true });
  await assert.rejects(md5(x));
  untouched(x);
});

test('unknown variants are refused; the name is looked up as an own key only', async () => {
  for (const name of ['nope', '', '__proto__', 'constructor', 'toString']) {
    const x = setup();
    await assert.rejects(md5(x, args({ stub_variant: name })), e => code(e) === 'stub_variant_unknown', name);
    assert.deepEqual(x.log.fetched, []); untouched(x);
  }
});

test('the variant cannot be used for anything but reads, nor with the ROM mode, whatever the server let through', async () => {
  for (const command of ['flash.write', 'flash.erase', 'partitions', 'identify', 'console', 'reset', 'uart.write']) {
    const x = setup();
    await assert.rejects(x.engine.execute(command, { ...args(), images: [], chip: 'esp32p4' }, x.context), e => code(e) === 'stub_variant_not_allowed', command);
    untouched(x);
  }
  const x = setup();
  await assert.rejects(md5(x, args({ loader: 'rom' })), e => code(e) === 'stub_variant_not_allowed'); untouched(x);
});

test('the variant is refused when the device is not a P4 (before the port is touched)', async () => {
  const x = setup({ deviceChip: 'esp32c3' });
  await assert.rejects(md5(x), e => code(e) === 'stub_variant_chip_mismatch');
  assert.deepEqual(x.log.fetched, []); untouched(x);
});

test('the variant is refused when the detected chip is not a P4 or is a v1.x silicon: nothing is uploaded, the session is closed', async () => {
  for (const options of [{ chip: 'ESP32-C3' }, { chip: 'ESP32-S3' }, { revision: 100 }, { revision: 130 }, { revision: 299 }]) {
    const x = setup(options);
    await assert.rejects(md5(x), e => code(e) === 'stub_variant_chip_mismatch', JSON.stringify(options));
    assert.equal(x.log.mem.length, 0, 'no memBegin'); assert.equal(x.log.finish.length, 0); assert.equal(x.engine.loader, null, 'no session left open'); assert.equal(x.log.md5, 0);
  }
  const ok = setup({ revision: 300 }); await md5(ok); assert.equal(ok.log.finish.length, 1);
});

test('a stub already running on the chip is never mixed with a variant', async () => {
  const x = setup({ syncStub: true });
  await assert.rejects(md5(x), e => code(e) === 'stub_variant_stub_running');
  assert.equal(x.log.mem.length, 0); assert.equal(x.engine.loader, null);
});

test('no OHAI after the upload: the variant did not start, the session is closed and no command follows', async () => {
  for (const greeting of ['', 'NOPE', 'OHA']) {
    const x = setup({ greeting });
    await assert.rejects(md5(x), e => code(e) === 'stub_variant_start_failed', greeting);
    assert.equal(x.engine.loader, null); assert.equal(x.log.md5, 0);
  }
});

test('a session already open with another stub is refused in both directions, before any fetch, and is not disturbed', async () => {
  const official = setup(); await md5(official, { address: 0x2000, size: 4096, return_console: false });
  const constructed = official.log.constructed.length;
  await assert.rejects(md5(official), e => code(e) === 'loader_mode_mismatch');
  assert.deepEqual(official.log.fetched, []); assert.equal(official.log.constructed.length, constructed); assert.ok(official.engine.loader, 'the official session is untouched');
  const variant = setup(); await md5(variant);
  await assert.rejects(md5(variant, { address: 0x2000, size: 4096, return_console: false }), e => code(e) === 'loader_mode_mismatch');
  await assert.rejects(md5(variant, { address: 0x2000, size: 4096, return_console: false, loader: 'rom' }), e => code(e) === 'loader_mode_mismatch');
  assert.equal(variant.log.mem.length, 2, 'the variant was uploaded once');
  const rom = setup(); await md5(rom, { address: 0x2000, size: 4096, return_console: false, loader: 'rom' });
  await assert.rejects(md5(rom), e => code(e) === 'loader_mode_mismatch');
});

test('a second command of the same variant session reuses it (identity strict, rank 2) without uploading again', async () => {
  const x = setup(); const a = await md5(x), b = await md5(x);
  assert.deepEqual([a.identify.identify_index, b.identify.identify_index], [1, 2]); assert.equal(x.log.mem.length, 2); assert.equal(x.log.fetched.length, 1);
  assert.equal(b.stub_identity.variant, NAME);
});

test('rollback: ending the variant session (console return) lets the next command use the official stub, no flash command involved', async () => {
  const x = setup();
  x.engine.openConsole = async function () { this.loader = null; this.loaderMode = null; this.loaderKey = null; this.loaderVariant = null; this.mode = 'console'; };
  const a = await md5(x, args({ return_console: true }));
  const b = await md5(x, { address: 0x2000, size: 4096, return_console: false });
  assert.equal(a.stub_identity.variant, NAME); assert.equal(b.stub_identity.variant, 'official'); assert.equal(x.log.runStub, 1);
});

// --- UART speed: the official stub can be asked for 115200 (the ROM's) so that a comparison with the variant is made at the same speed -----------------------------
test('uart_baud 115200 makes the OFFICIAL stub run at 115200: the real runStub is used, nothing is fetched, the identity reports the speed, the default stays 460800', async () => {
  const x = setup();
  const r = await md5(x, { address: 0x2000, size: 4096, return_console: false, uart_baud: 115200 });
  assert.deepEqual(x.log.constructed, [{ baudrate: 115200 }]); assert.equal(x.log.runStub, 1); assert.deepEqual(x.log.fetched, []);
  assert.equal(r.stub_identity.variant, 'official'); assert.equal(r.stub_identity.uart_baud, 115200);
  const y = setup(); const d = await md5(y, { address: 0x2000, size: 4096, return_console: false });
  assert.deepEqual(y.log.constructed, [{ baudrate: 460800 }]); assert.equal(d.stub_identity.uart_baud, 460800);
});

test('the variant is at 115200 whether or not the argument is given, and no other speed is accepted anywhere', async () => {
  const a = setup(), b = setup();
  assert.equal((await md5(a)).stub_identity.uart_baud, 115200); assert.equal((await md5(b, args({ uart_baud: 115200 }))).stub_identity.uart_baud, 115200);
  for (const bad of [460800, 921600, 9600, 0, -1, '115200', true, null, 115200.5]) {
    for (const extra of [{}, V_ARGS]) {
      const x = setup();
      await assert.rejects(md5(x, { address: 0x2000, size: 4096, return_console: false, ...extra, uart_baud: bad }), e => code(e) === 'uart_baud_not_allowed', String(bad));
      untouched(x); assert.deepEqual(x.log.fetched, []);
    }
  }
});

const V_ARGS = { stub_variant: NAME, stub_sha256: ENTRY.sha256 };

test('uart_baud is for the two reads and flash.write only, and never with the ROM loader', async () => {
  for (const command of ['flash.erase', 'partitions', 'identify', 'console', 'reset', 'mark']) {
    const x = setup();
    await assert.rejects(x.engine.execute(command, { uart_baud: 115200, images: [], chip: 'esp32p4' }, x.context), e => code(e) === 'uart_baud_not_allowed', command);
    untouched(x);
  }
  const x = setup();
  await assert.rejects(md5(x, { address: 0x2000, size: 4096, return_console: false, loader: 'rom', uart_baud: 115200 }), e => code(e) === 'uart_baud_not_allowed'); untouched(x);
});

test('the speed is part of the session: a session opened at one speed is never reused for another, in both directions, and is reused for the same one', async () => {
  const slow = setup(); await md5(slow, { address: 0x2000, size: 4096, return_console: false, uart_baud: 115200 });
  await assert.rejects(md5(slow, { address: 0x2000, size: 4096, return_console: false }), e => code(e) === 'loader_mode_mismatch');
  const again = await md5(slow, { address: 0x2000, size: 4096, return_console: false, uart_baud: 115200 });
  assert.equal(again.identify.identify_index, 2); assert.equal(slow.log.constructed.length, 1);
  const fast = setup(); await md5(fast, { address: 0x2000, size: 4096, return_console: false });
  await assert.rejects(md5(fast, { address: 0x2000, size: 4096, return_console: false, uart_baud: 115200 }), e => code(e) === 'loader_mode_mismatch');
  assert.equal(fast.log.constructed.length, 1);
});

test('flash.write accepts uart_baud 115200 (the official stub then runs at 115200) and still refuses any other speed before the port is touched', async () => {
  const x = setup();
  await assert.rejects(x.engine.execute('flash.write', { uart_baud: 460800, images: [], chip: 'esp32p4' }, x.context), e => code(e) === 'uart_baud_not_allowed'); untouched(x);
  // the guard lets 115200 through: the session is opened at 115200 (the double has no write support, so the command fails later, after that)
  const y = setup();
  x.context.download = async () => new Uint8Array(4096);
  await y.engine.execute('flash.write', { uart_baud: 115200, chip: 'esp32p4', images: [] }, { ...y.context, download: async () => new Uint8Array(4096) }).catch(() => {});
  assert.deepEqual(y.log.constructed, [{ baudrate: 115200 }]);
});

// Native USB ports of the ESP32-P4 (303a:0012 ROM USB-OTG, 303a:1001 USB-Serial-JTAG): the engine's REAL openLoader()/openConsole() with esptool-js and the serial port replaced by doubles.
// It proves the engine's decisions (loader speed, entry mode, console reopening); it proves nothing about the real chip, the USB port or esptool-js.
import test from 'node:test';
import assert from 'node:assert/strict';
import { EspEngine } from '../web/esp-engine.js';
import { ESPError, FakeTransport } from './fakes.mjs';

const P4_JEDEC = 0x1940C8;

function setup({ vid = 0x303a, pid = 0x0012, manual = false } = {}) {
  const log = { modes: [], baud: [], opened: [] };
  class FakeLoader {
    constructor(options) { log.baud.push(options.baudrate); this.transport = options.transport; this.IS_STUB = false; this.chip = { CHIP_NAME: 'ESP32-P4', getChipRevision: async () => 302, readMac: async () => 'aa:bb:cc:dd:ee:ff' }; }
    async main(mode) { log.modes.push(mode); return 'ESP32-P4 (revision v3.2)'; }
    async readFlashId() { return P4_JEDEC; }
  }
  const events = [];
  const readable = { getReader() { let release; const pending = new Promise(resolve => { release = resolve; }); return { read: () => pending, cancel: async () => release({ done: true }), releaseLock() {} }; } };
  const port = { getInfo: () => ({ usbVendorId: vid, usbProductId: pid }), open: async options => { log.opened.push(options); }, close: async () => {}, setSignals: async () => {}, readable, writable: null };
  const engine = new EspEngine(port, { ESPLoader: FakeLoader, Transport: FakeTransport, ESPError }, (type, payload) => events.push({ type, payload }));
  engine.manualDownload = manual;
  return { engine, log, events };
}

test('default: esptool-js keeps its own reset sequence (main() gets the default mode), whatever the port', async () => {
  for (const [vid, pid] of [[0x303a, 0x1001], [0x303a, 0x0012], [0x1a86, 0x55d3]]) {
    const x = setup({ vid, pid });
    await x.engine.openLoader();
    assert.deepEqual(x.log.modes, ['default_reset'], `${vid.toString(16)}:${pid.toString(16)}`);
  }
});

test('opt-in on a native USB port (303a:0012 ROM USB-OTG, 303a:1001 USB-Serial-JTAG, any 303a:*): no reset, and the choice is announced as an event', async () => {
  for (const [vid, pid] of [[0x303a, 0x0012], [0x303a, 0x1001], [0x303a, 0x4001]]) {
    const x = setup({ vid, pid, manual: true });
    await x.engine.openLoader();
    assert.deepEqual(x.log.modes, ['no_reset'], `${vid.toString(16)}:${pid.toString(16)}`);
    assert.ok(x.events.some(e => e.type === 'transport' && /without reset/.test(e.payload.detail)));
  }
});

test('the opt-in never applies to an external UART bridge: it keeps the reset sequence', async () => {
  for (const [vid, pid] of [[0x1a86, 0x55d3], [0x10c4, 0xea60], [0x0403, 0x6001]]) {
    const x = setup({ vid, pid, manual: true });
    await x.engine.openLoader();
    assert.deepEqual(x.log.modes, ['default_reset'], `${vid.toString(16)}:${pid.toString(16)}`);
    assert.equal(x.engine.keepsLoaderOpen(), false);
  }
});

test('loader speed: native USB ports stay at 115200 (no esptool-js baud change); an external UART bridge keeps 460800', async () => {
  for (const [vid, pid, expected] of [[0x303a, 0x0012, 115200], [0x303a, 0x1001, 115200], [0x1a86, 0x55d3, 460800], [0x10c4, 0xea60, 460800]]) {
    const x = setup({ vid, pid });
    const id = await x.engine.identity();
    assert.deepEqual(x.log.baud, [expected], `${vid.toString(16)}:${pid.toString(16)}`);
    assert.equal(id.chip, 'esp32p4');
  }
});

test('the identification of a connection uses the same entry mode', async () => {
  const x = setup({ manual: true });
  const identity = await x.engine.identity();
  assert.equal(identity.chip, 'esp32p4'); assert.deepEqual(x.log.modes, ['no_reset']);
});

test('opt-in on a native USB port: the loader session stays open (the console is not reopened after identification, nor after a command), an explicit console command still goes to the console', async () => {
  const x = setup({ manual: true });
  await x.engine.identity();
  assert.equal(x.engine.keepsLoaderOpen(), true);
  await x.engine.openConsole();                                       // what the connection does after identification, and a command with return_console
  assert.equal(x.engine.mode, 'loader'); assert.deepEqual(x.log.opened, [], 'the port was not closed and reopened');
  await x.engine.openConsole(true);                                   // an explicit console command / uart.configure leaves the loader session
  assert.equal(x.engine.mode, 'console'); assert.equal(x.log.opened.length, 1);
});

test('without the opt-in the console is reopened as before, on every port', async () => {
  for (const [vid, pid] of [[0x303a, 0x0012], [0x1a86, 0x55d3]]) {
    const x = setup({ vid, pid, manual: false });
    await x.engine.identity();
    assert.equal(x.engine.keepsLoaderOpen(), false);
    await x.engine.openConsole();
    assert.equal(x.engine.mode, 'console');
  }
});

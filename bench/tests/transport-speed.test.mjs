import test from 'node:test';
import assert from 'node:assert/strict';
import { effectiveLoaderBaud, flashTransfer } from '../web/loader-settings.js';
import { EspEngine } from '../web/esp-engine.js';
import { readFileSync } from 'node:fs';

test('UART bridge and unknown VID default to 115200, independently for every board', () => {
  for (const [vid, pid] of [[0x1a86,0x7523],[0x10c4,0xea60],[0x0403,0x6001],[0x067b,0x2303],[0xffff,0x0001]]) {
    const port = { getInfo: () => ({ usbVendorId: vid, usbProductId: pid }) };
    const engine = new EspEngine(port, {}, () => {});
    assert.equal(engine.native, false);
    assert.equal(engine.flashBaud, 115200);
    assert.equal(effectiveLoaderBaud(engine.native, engine.loaderBaud, engine.flashBaud), 115200);
  }
  assert.equal(effectiveLoaderBaud(false, 230400), 230400, 'explicit override remains supported');
});

test('Espressif native USB retains nominal CDC speed, never an UART speed escalation', () => {
  for (const pid of [0x1001,0x0012,0x4001]) {
    const engine = new EspEngine({getInfo:()=>({usbVendorId:0x303a,usbProductId:pid})},{},()=>{});
    assert.equal(engine.native,true);
    assert.equal(effectiveLoaderBaud(engine.native, 921600, engine.flashBaud),115200);
    assert.equal(engine.flashBaud,115200);
  }
});

test('measured useful transfer throughput is exact, excludes protocol speed claims', () => {
  assert.deepEqual(flashTransfer(2048,2000),{bytes:2048,elapsed_ms:2000,bytes_per_second:1024});
  assert.deepEqual(flashTransfer(512,0),{bytes:512,elapsed_ms:0,bytes_per_second:512000});
  for(const [bytes,elapsed] of [[-1,1],[1,-1],[Infinity,1],[1,NaN]])
    assert.throws(()=>flashTransfer(bytes,elapsed),/flash_transfer_invalid/);
});

test('UI exposes connection speed and useful transfer without labeling USB as UART', () => {
  const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  const ui=readFileSync(new URL('../web/ui.js',import.meta.url),'utf8');
  for(const id of ['interface-info','loader-speed-info','console-speed-info','transfer-rate-info'])
    assert.match(html,new RegExp('id="'+id+'"'));
  assert.match(ui,/USB direct · 115 200 nominal \(sans UART\)/);
  assert.match(ui,/effectiveLoaderBaud\(false, engine\.loaderBaud, engine\.flashBaud\)/);
  assert.match(ui,/flashRates\.set\(op\.device/);
  assert.match(ui,/transfer\.bytes_per_second \/ 1024/);
});

// Software-only integration: existing ROM admission + RAM load + fixed PING/PONG.
import test from 'node:test';
import assert from 'node:assert/strict';
import {P4_BRINGUP, expectedBringupConsent, runP4UsbBringup, validateBringupArgs} from '../web/p4-bringup.js';
import {sha256} from '../web/protocol.js';

const stubText = JSON.stringify({entry: 0x4ff50010,
  text: Buffer.alloc(100, 0x13).toString('base64'), text_start: 0x4ff50000,
  data: Buffer.alloc(40, 7).toString('base64'), data_start: 0x4ff60040,
  bss_start: 0x4ff60000});
const bytes = new TextEncoder().encode(stubText), sha = await sha256(bytes);
const device = {id: 'p4', chip: 'esp32p4'};
const args = () => ({chip: 'esp32p4', stub_artifact: 'artifact',
  stub_sha256: sha, scenario: 'ohai_ping2', consent: expectedBringupConsent('p4', sha)});

function fake({fault, revision = 302} = {}) {
  const calls = [];
  let count = 0;
  const loader = {
    IS_STUB: false, ESP_RAM_BLOCK: 0x1800, DEFAULT_TIMEOUT: 1000, calls,
    chip: {CHIP_NAME: 'ESP32-P4', USB_RAM_BLOCK: 0x800, getChipRevision: async () => revision},
    usesUsbOtg: async () => true,
    sync: async () => {loader.syncStubDetected = false; calls.push('sync');},
    pending: [],
    command: async op => {
      calls.push(['sync_frame', op]); loader.pending = Array(7).fill(1);
      return [1, new Uint8Array(0)];
    },
    readPacket: async () => [loader.pending.shift(), new Uint8Array(0)],
    memBegin: async () => {calls.push('memBegin');},
    memBlock: async () => {calls.push('memBlock'); if (fault === 'mem_failure') throw new Error('closed');},
    memFinish: async () => {calls.push('memFinish');},
    checkCommand: async () => {calls.push('FORBIDDEN_LEGACY_OPCODE'); throw new Error('forbidden');},
    flashBegin: async () => {calls.push('FORBIDDEN_FLASH'); throw new Error('forbidden');},
    transport: {
      flushInput() {calls.push('flush');},
      async read() {
        calls.push('read');
        if (!loader.IS_STUB) return Uint8Array.from(fault === 'bad_ohai' ? 'BAD!' : 'OHAI', c => c.charCodeAt(0));
        count++;
        if (fault === 'timeout' && count === 2) throw new Error('timeout');
        return Uint8Array.from(fault === 'bad_pong' && count === 2 ? 'NOPE' : 'PONG', c => c.charCodeAt(0));
      },
      async write(payload) {calls.push(['write', Array.from(payload)]);},
    },
  };
  return loader;
}

test('normal: ROM-only RAM load, OHAI and exactly two PING/PONG on the same transport', async () => {
  const loader = fake(), transport = loader.transport;
  const result = await runP4UsbBringup({loader, args: args(), device, stubBytes: bytes, romOnly: true});
  assert.equal(result.greeting, 'OHAI');
  assert.equal(result.exchanges.length, 2);
  assert.ok(result.exchanges.every(e => e.passed));
  assert.equal(result.stub_resident, true);
  assert.equal(loader.transport, transport);
  assert.equal(loader.__idlabBringupStub, true);
  assert.equal(loader.calls.filter(x => x === 'FORBIDDEN_LEGACY_OPCODE' || x === 'FORBIDDEN_FLASH').length, 0);
  assert.deepEqual(loader.calls.filter(x => Array.isArray(x) && x[0] === 'write'),
    [['write', [80, 73, 78, 71]], ['write', [80, 73, 78, 71]]]);
  assert.equal(loader.calls.filter(x => x === 'memFinish').length, 1);
  assert.equal(loader.calls.filter(x => x === 'read').length, 3);
});

test('bad consent or SHA never touches ROM', async () => {
  for (const overrides of [
    {consent: {...expectedBringupConsent('other', sha)}},
    {scenario: 'arbitrary'}, {stub_sha256: '0'.repeat(64)},
  ]) {
    const loader = fake();
    await assert.rejects(runP4UsbBringup({loader, args: {...args(), ...overrides},
      device, stubBytes: bytes, romOnly: true}), e => e.indeterminate === false);
    assert.equal(loader.calls.length, 0);
  }
  assert.throws(() => validateBringupArgs({...args(), extra: true}, device), /diagnostic_parameter_not_allowed/);
});

test('greeting failure does not send PING or retry RAM loading', async () => {
  const loader = fake({fault: 'bad_ohai'});
  await assert.rejects(runP4UsbBringup({loader, args: args(), device, stubBytes: bytes, romOnly: true}),
    e => e.indeterminate === true);
  assert.equal(loader.calls.filter(x => x === 'memFinish').length, 1);
  assert.equal(loader.calls.filter(x => Array.isArray(x) && x[0] === 'write').length, 0);
});

test('wrong second PONG blocks completion without a third PING', async () => {
  const loader = fake({fault: 'bad_pong'});
  await assert.rejects(runP4UsbBringup({loader, args: args(), device, stubBytes: bytes, romOnly: true}),
    e => e.indeterminate === true && e.code === 'diagnostic_bringup_unexpected_pong');
  assert.equal(loader.calls.filter(x => Array.isArray(x) && x[0] === 'write').length, 2);
  assert.equal(loader.calls.filter(x => x === 'memFinish').length, 1);
});

test('lost ROM RAM transfer remains indeterminate, never retried', async () => {
  const loader = fake({fault: 'mem_failure'});
  await assert.rejects(runP4UsbBringup({loader, args: args(), device, stubBytes: bytes, romOnly: true}),
    e => e.indeterminate === true);
  assert.equal(loader.calls.filter(x => x === 'memFinish').length, 0);
  assert.equal(loader.calls.filter(x => x === 'memBlock').length, 1);
});

test('bring-up rejects P4 revision 3.1 before the first MEM_BEGIN', async () => {
  const loader = fake({revision: 301});
  await assert.rejects(runP4UsbBringup({
    loader, args: args(), device, stubBytes: bytes, romOnly: true,
  }), e => e.code === 'diagnostic_stub_unsupported_chip_revision' && e.indeterminate === false);
  assert.equal(loader.calls.filter(x => /^mem/.test(String(x))).length, 0);
});

test('expiration after first MEM_BEGIN prevents MEM_DATA and MEM_END', async () => {
  const loader = fake();
  let live = true;
  loader.memBegin = async () => { loader.calls.push('memBegin'); live = false; };
  await assert.rejects(runP4UsbBringup({
    loader, args: args(), device, stubBytes: bytes, romOnly: true,
    check: () => { if (!live) throw Object.assign(new Error('lease expired'), {code: 'reservation_expired'}); },
  }), e => e.indeterminate === true);
  assert.equal(loader.calls.filter(x => x === 'memBegin').length, 1);
  assert.equal(loader.calls.filter(x => x === 'memBlock').length, 0);
  assert.equal(loader.calls.filter(x => x === 'memFinish').length, 0);
});

test('expiration directly before MEM_END refuses candidate execution', async () => {
  const loader = fake();
  let live = true;
  loader.memBlock = async () => { loader.calls.push('memBlock'); live = false; };
  await assert.rejects(runP4UsbBringup({
    loader, args: args(), device, stubBytes: bytes, romOnly: true,
    check: () => { if (!live) throw Object.assign(new Error('lease expired'), {code: 'reservation_expired'}); },
  }), e => e.indeterminate === true);
  assert.equal(loader.calls.filter(x => x === 'memBegin').length, 1);
  assert.equal(loader.calls.filter(x => x === 'memBlock').length, 1);
  assert.equal(loader.calls.filter(x => x === 'memFinish').length, 0);
});

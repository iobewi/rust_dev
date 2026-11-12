import test from 'node:test';
import assert from 'node:assert/strict';
import { EspEngine } from '../web/esp-engine.js';
import { hardwareError, diagnostic } from '../web/hardware-errors.js';

const MiB = 1024 * 1024;
const P4_JEDEC = 0x1940C8;          // the value the P4 returned on its first identification (capacity byte 0x19 = 32 MiB)

// A loader double: readFlashId answers from a script, everything else is counted so that "no retry" can be asserted.
function setup(ids, { stub = true } = {}) {
  const calls = { readFlashId: 0, readFlash: 0, readMac: 0 };
  const loader = {
    IS_STUB: stub,
    async readFlashId() { return ids[Math.min(calls.readFlashId++, ids.length - 1)]; },
    async readFlash(_address, size) { calls.readFlash++; return new Uint8Array(size).fill(7); },
    chip: { async readMac() { calls.readMac++; return 'aa:bb:cc:dd:ee:ff'; } },
  };
  const engine = new EspEngine({ getInfo: () => ({}) }, {}, () => {});
  engine.loader = loader; engine.chipDescription = 'ESP32-P4 (revision v1.0)';
  const context = { check() {}, live: () => true, checkLive() {}, device: { chip: 'esp32p4', flash_size: 32 * MiB },
    report() {}, upload: async data => ({ size: data.length }) };
  return { engine, loader, calls, context };
}
const read = (x) => x.engine.execute('flash.read', { address: 0x2000, size: 4096, return_console: false }, x.context);

test('a valid first identification is unchanged and counts as the first check of the loader session', async () => {
  const x = setup([P4_JEDEC]);
  const id = await x.engine.identity();
  assert.deepEqual({ chip: id.chip, flash_size: id.flash_size, flash_id: id.flash_id }, { chip: 'esp32p4', flash_size: 32 * MiB, flash_id: P4_JEDEC });
  assert.equal(x.engine.identityChecks, 1);
});

test('the second identification of a loader session that reads an invalid JEDEC id reports the raw value and its rank, without retry', async () => {
  const x = setup([P4_JEDEC, 0x000000]);
  const first = await read(x);
  assert.equal(first.artifact.size, 4096);
  await assert.rejects(read(x), error => {
    const d = hardwareError(error);
    assert.equal(d.code, 'flash_capacity_unknown');
    assert.equal(d.phase, 'identify');
    assert.equal(d.jedec_id, 0);
    assert.equal(d.identify_index, 2);
    assert.equal(d.loader_stub, 1);
    assert.notEqual(d.code, 'physical_identity_changed', 'an unreadable id is not an identity mismatch');
    return true;
  });
  assert.equal(x.calls.readFlashId, 2, 'the failed identification is not repeated');
  assert.equal(x.calls.readFlash, 1, 'no flash read follows a failed identification');
  assert.equal(x.calls.readMac, 1, 'the MAC is read only after a valid id (first command)');
});

test('the identity check stays strict: no cache, the id is read again for every command', async () => {
  const x = setup([P4_JEDEC, P4_JEDEC, P4_JEDEC]);
  await read(x); await read(x); await read(x);
  assert.equal(x.calls.readFlashId, 3);
  assert.equal(x.engine.identityChecks, 3);
});

test('a different valid capacity is still a physical identity change', async () => {
  const x = setup([P4_JEDEC, 0x174068]);        // 8 MiB class id on a card declared as 32 MiB
  await read(x);
  await assert.rejects(read(x), error => hardwareError(error).code === 'physical_identity_changed');
});

test('capacity byte boundaries: 12..26 are accepted, 11, 27 and 255 are refused with the raw id', async () => {
  for (const [byte, valid] of [[11, false], [12, true], [26, true], [27, false], [255, false], [0, false]]) {
    const x = setup([byte << 16 | 0x4068]);
    if (valid) { assert.equal((await x.engine.identity()).flash_size, 2 ** byte); continue; }
    await assert.rejects(x.engine.identity(), error => {
      const d = hardwareError(error); return d.code === 'flash_capacity_unknown' && d.jedec_id === (byte << 16 | 0x4068);
    });
  }
});

test('a floating or all-ones read is reported as a 24-bit raw value', async () => {
  const x = setup([0xFFFFFFFF]);
  await assert.rejects(x.engine.identity(), error => hardwareError(error).jedec_id === 0xFFFFFFFF);
});

test('the diagnostic allowlist passes the numeric fields only and never arbitrary text', () => {
  const d = hardwareError({ code: 'flash_capacity_unknown', detail: { phase: 'identify', jedec_id: 5, identify_index: 2, loader_stub: 0, secret: 'x', jedec_text: 'abc' } });
  assert.deepEqual([d.jedec_id, d.identify_index, d.loader_stub, d.secret, d.jedec_text], [5, 2, 0, undefined, undefined]);
  const bad = hardwareError({ code: 'flash_capacity_unknown', detail: { jedec_id: 'abc', identify_index: -1 } });
  assert.equal(bad.jedec_id, undefined); assert.equal(bad.identify_index, undefined);
  assert.match(diagnostic('flash_capacity_unknown'), /JEDEC/);
});

test('a new loader session restarts the rank', async () => {
  const x = setup([P4_JEDEC]);
  await x.engine.identity(); await x.engine.identity();
  assert.equal(x.engine.identityChecks, 2);
  x.engine.identityChecks = 0;                  // what openLoader() does when it creates a new ESPLoader
  await x.engine.identity();
  assert.equal(x.engine.identityChecks, 1);
});

test('a successful flash.read reports the raw JEDEC id of its own identification, and the check stays strict', async () => {
  const x = setup([P4_JEDEC, P4_JEDEC]);
  const first = await read(x), second = await read(x);
  assert.deepEqual(first.identify, { jedec_id: P4_JEDEC, identify_index: 1, loader_stub: 1, chip_description: 'ESP32-P4 (revision v1.0)' });
  assert.deepEqual(second.identify, { jedec_id: P4_JEDEC, identify_index: 2, loader_stub: 1, chip_description: 'ESP32-P4 (revision v1.0)' });
  assert.equal(first.artifact.size, 4096);
  assert.equal(x.calls.readFlashId, 2, 'the id is still read again for every command');
});

test('a successful partitions command also reports the JEDEC id read for it', async () => {
  const x = setup([P4_JEDEC]);
  x.loader.readFlash = async (_a, size) => { const b = new Uint8Array(size).fill(0xFF); b.set([0xAA, 0x50, 0, 2, 0, 0x10, 0, 0, 0, 0x10, 0, 0], 0); b.set(new TextEncoder().encode('nvs'), 12); return b; };
  const result = await x.engine.execute('partitions', { address: 0x8000, return_console: false }, x.context);
  assert.deepEqual(result.identify, { jedec_id: P4_JEDEC, identify_index: 1, loader_stub: 1, chip_description: 'ESP32-P4 (revision v1.0)' });
  assert.equal(result.partitions.length, 1);
});

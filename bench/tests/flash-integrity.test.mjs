import test from 'node:test';
import assert from 'node:assert/strict';
import { EspEngine } from '../web/esp-engine.js';
import { hardwareError } from '../web/hardware-errors.js';

const MiB = 1024 * 1024;
const P4_JEDEC = 0x1940C8;
const HEX = 'ab'.repeat(16);

// Loader double. `frames` scripts what transport.read() returns after each readFlash (a Uint8Array, or an error `kind`). `md5` is what flashMd5sum answers.
function setup({ frames = [], md5 = HEX, stub = true, md5Error = null } = {}) {
  const calls = { readFlash: 0, flashMd5sum: 0, transportRead: 0, readFlashId: 0 };
  const loader = {
    IS_STUB: stub,
    transport: { async read() {
      const next = frames[calls.transportRead++];
      if (next && next.kind) throw Object.assign(new Error(next.kind), { kind: next.kind });
      return next;
    } },
    async readFlashId() { calls.readFlashId++; return P4_JEDEC; },
    async readFlash(_a, size) { calls.readFlash++; return new Uint8Array(size).fill(7); },
    async flashMd5sum(a, s) { calls.flashMd5sum++; if (md5Error) throw md5Error; return typeof md5 === 'function' ? md5(a, s) : md5; },
    chip: { async readMac() { return 'aa:bb:cc:dd:ee:ff'; } },
  };
  const engine = new EspEngine({ getInfo: () => ({}) }, {}, () => {});
  engine.loader = loader; engine.chipDescription = 'ESP32-P4 (revision v1.0)';
  const context = { check() {}, live: () => true, checkLive() {}, device: { chip: 'esp32p4', flash_size: 32 * MiB }, report() {}, upload: async data => ({ size: data.length }) };
  return { engine, loader, calls, context };
}
const run = (x, command, args) => x.engine.execute(command, { return_console: false, ...args }, x.context);
const digest = (n) => Uint8Array.from({ length: 16 }, () => n);

// --- trailingDigest ----------------------------------------------------------------------------------------------------
test('trailingDigest classifies a 16-byte frame as received, with its hex', async () => {
  const x = setup({ frames: [digest(0xab)] });
  assert.deepEqual(await x.engine.trailingDigest(x.loader), { state: 'received', md5: HEX });
});

test('trailingDigest classifies no-response, partial-packet and invalid-packet as observed, and a wrong length as partial or invalid', async () => {
  for (const [frame, state] of [[{ kind: 'no-response' }, 'absent'], [{ kind: 'partial-packet' }, 'partial'], [{ kind: 'invalid-packet' }, 'invalid'],
                                [new Uint8Array(9), 'partial'], [new Uint8Array(20), 'invalid'], ['not bytes', 'invalid']]) {
    const x = setup({ frames: [frame] });
    assert.equal((await x.engine.trailingDigest(x.loader)).state, state);
  }
});

test('a closed port is a disconnection, never classified as an absent digest', async () => {
  const x = setup({ frames: [{ kind: 'closed' }] });
  await assert.rejects(x.engine.trailingDigest(x.loader), error => error.kind === 'closed');
});

// --- flash.read ----------------------------------------------------------------------------------------------------------
test('flash.read without stub_digest is unchanged: no extra transport read, no stub_digests field', async () => {
  const x = setup();
  const result = await run(x, 'flash.read', { address: 0x2000, size: 4096 });
  assert.equal(x.calls.transportRead, 0);
  assert.ok(!('stub_digests' in result));
  assert.equal(result.artifact.size, 4096);
});

test('flash.read with stub_digest false behaves like absent (strict true only)', async () => {
  const x = setup();
  const result = await run(x, 'flash.read', { address: 0x2000, size: 4096, stub_digest: false });
  assert.equal(x.calls.transportRead, 0); assert.ok(!('stub_digests' in result));
});

test('flash.read with stub_digest reads one trailing frame per 1 MiB block and reports each state', async () => {
  const x = setup({ frames: [digest(1), digest(2), { kind: 'no-response' }] });
  const result = await run(x, 'flash.read', { address: 0x100000, size: 2 * MiB + 4096, stub_digest: true });
  assert.equal(x.calls.readFlash, 3);
  assert.deepEqual(result.stub_digests.map(d => [d.block_address, d.block_size, d.state]),
    [[0x100000, MiB, 'received'], [0x200000, MiB, 'received'], [0x300000, 4096, 'absent']]);
  assert.equal(result.stub_digests[0].md5, '01'.repeat(16));
});

test('an absent digest does not turn the read into a failure and nothing is retried', async () => {
  const x = setup({ frames: [{ kind: 'no-response' }] });
  const result = await run(x, 'flash.read', { address: 0x2000, size: 4096, stub_digest: true });
  assert.equal(result.stub_digests[0].state, 'absent');
  assert.equal(x.calls.readFlash, 1); assert.equal(x.calls.transportRead, 1);
});

// --- flash.md5 -----------------------------------------------------------------------------------------------------------------
test('flash.md5 with the stub returns the 16-byte digest as 32 hex characters and the identification', async () => {
  const x = setup();
  const result = await run(x, 'flash.md5', { address: 0x2000, size: 4096 });
  assert.deepEqual({ md5: result.md5, format: result.format, address: result.address, size: result.size }, { md5: HEX, format: 'stub-raw16', address: 0x2000, size: 4096 });
  assert.equal(result.identify.jedec_id, P4_JEDEC);
  assert.equal(x.calls.flashMd5sum, 1);
});

test('flash.md5 with the ROM decodes the 32 ASCII characters', async () => {
  const ascii = Array.from('0123456789abcdef0123456789abcdef', c => c.charCodeAt(0).toString(16)).join('');
  const x = setup({ stub: false, md5: ascii });
  assert.equal((await run(x, 'flash.md5', { address: 0x2000, size: 4096 })).md5, '0123456789abcdef0123456789abcdef');
});

test('an invalid chip answer is md5_response_invalid, never an empty or default MD5', async () => {
  for (const bad of ['', 'zz'.repeat(16), 'ab'.repeat(15), undefined, null, 5, 'AB'.repeat(16)]) {
    const x = setup({ md5: () => bad });
    await assert.rejects(run(x, 'flash.md5', { address: 0x2000, size: 4096 }), error => {
      const d = hardwareError(error); return d.code === 'md5_response_invalid' && d.phase === 'flash_md5';
    }, `answer ${String(bad)}`);
    assert.equal(x.calls.flashMd5sum, 1, 'no retry');
  }
  const x = setup({ stub: false, md5: 'ab'.repeat(16) });             // ROM expects ASCII hex digits, not arbitrary bytes
  await assert.rejects(run(x, 'flash.md5', { address: 0x2000, size: 4096 }), error => hardwareError(error).code === 'md5_response_invalid');
});

test('a command error from the chip propagates and is not retried', async () => {
  const x = setup({ md5Error: Object.assign(new Error('Failed to read flash md5'), { name: 'ESPError' }) });
  await assert.rejects(run(x, 'flash.md5', { address: 0x2000, size: 4096 }));
  assert.equal(x.calls.flashMd5sum, 1);
});

test('flash.md5 refuses a range outside the flash before any chip command', async () => {
  const x = setup();
  await assert.rejects(run(x, 'flash.md5', { address: 32 * MiB - 4096, size: 8192 }));
  assert.equal(x.calls.flashMd5sum, 0);
});

test('flash.md5 keeps the strict identity check (id read again, a different capacity is refused)', async () => {
  const x = setup();
  x.loader.readFlashId = async () => 0x174068;
  await assert.rejects(run(x, 'flash.md5', { address: 0x2000, size: 4096 }), error => hardwareError(error).code === 'physical_identity_changed');
  assert.equal(x.calls.flashMd5sum, 0);
});

test('the engine announces the flash.md5 capability', () => {
  assert.ok(setup().engine.capabilities().includes('flash.md5'));
});

test('after a digest that is not received, no further frame is read: the remaining blocks are not_attempted and the data is still read', async () => {
  const x = setup({ frames: [{ kind: 'partial-packet' }, digest(2), digest(3)] });
  const result = await run(x, 'flash.read', { address: 0x100000, size: 3 * MiB, stub_digest: true });
  assert.deepEqual(result.stub_digests.map(d => d.state), ['partial', 'not_attempted', 'not_attempted']);
  assert.equal(x.calls.transportRead, 1, 'one transport read only');
  assert.equal(x.calls.readFlash, 3, 'the data blocks are all read as without the option');
  assert.equal(result.artifact.size, 3 * MiB);
});

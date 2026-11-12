// The trailing MD5 frame against the REAL BenchTransport SLIP parser (only the serial port is a fake), with the read loop of esptool-js 0.7.0 copied
// literally (ESPLoader.readFlash: read a packet, append, write the cumulative 4-byte acknowledgement, until the length is reached).
// What this shows: how the frame is consumed by the bench's parser for the byte sequences below. What it does NOT show: that the real stub
// sends this frame, when it sends it, or with which framing; that stays to be observed on hardware.
import test from 'node:test';
import assert from 'node:assert/strict';
import { makeBenchTransport } from '../web/bench-transport.js';
import { EspEngine } from '../web/esp-engine.js';
import { ESPError, FakeTransport, feed } from './fakes.mjs';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const make = () => {
  const { BenchTransport } = makeBenchTransport(FakeTransport, ESPError);
  let end; const device = { pumpDone: new Promise((r) => { end = r; }) };
  return { t: new BenchTransport(device), close: end };
};
const frame = FakeTransport.frame;
const MD5 = Uint8Array.from({ length: 16 }, (_, i) => 0x10 + i);
const hex = (b) => Array.from(b, x => x.toString(16).padStart(2, '0')).join('');

// ESPLoader.readFlash 0.7.0 after the READ_FLASH response, verbatim in behaviour.
async function esptoolReadLoop(t, length, acks) {
  let h = new Uint8Array(0);
  for (; h.length < length;) {
    const p = await t.read(2000);
    if (p.length > 0) { h = t.appendArray(h, p); acks.push(h.length); }
  }
  return h;
}
const engine = () => new EspEngine({ getInfo: () => ({}) }, {}, () => {});

test('data packet and MD5 frame in the SAME chunk: the loop stops at the length, the next read returns exactly the 16 bytes', async () => {
  const { t } = make(), acks = [];
  const data = new Uint8Array(4096).fill(7);
  feed(t, Uint8Array.from([...frame(data), ...frame(MD5)]));
  assert.equal((await esptoolReadLoop(t, 4096, acks)).length, 4096);
  assert.deepEqual(acks, [4096]);
  assert.deepEqual(await engine().trailingDigest({ transport: t }), { state: 'received', md5: hex(MD5) });
});

test('the MD5 frame arriving later, in pieces, after the acknowledgement is received whole', async () => {
  const { t } = make(), acks = [];
  feed(t, frame(new Uint8Array(4096).fill(1)));
  await esptoolReadLoop(t, 4096, acks);
  const raw = frame(MD5);
  setTimeout(() => feed(t, raw.slice(0, 5)), 20); setTimeout(() => feed(t, raw.slice(5, 11)), 40); setTimeout(() => feed(t, raw.slice(11)), 60);
  assert.equal((await engine().trailingDigest({ transport: t })).md5, hex(MD5));
});

test('a digest containing SLIP special bytes (0xc0, 0xdb) is unescaped to its 16 bytes', async () => {
  const { t } = make();
  const special = Uint8Array.from([0xc0, 1, 2, 0xdb, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 0xc0, 0xdb]);
  feed(t, frame(special));
  assert.equal((await engine().trailingDigest({ transport: t })).md5, hex(special));
});

test('a multi-packet block (several 4096-byte frames) then the MD5: the digest is the first frame AFTER the data and nothing is left', async () => {
  const { t } = make(), acks = [];
  feed(t, Uint8Array.from([...frame(new Uint8Array(4096).fill(1)), ...frame(new Uint8Array(4096).fill(2)), ...frame(MD5)]));
  assert.equal((await esptoolReadLoop(t, 8192, acks)).length, 8192);
  assert.deepEqual(acks, [4096, 8192]);
  assert.equal((await engine().trailingDigest({ transport: t })).md5, hex(MD5));
  assert.equal(t.buffer.length, 0, 'the whole stream was consumed');
});

test('no frame at all: absent after the timeout; the closed port is still a disconnection', async () => {
  const { t, close } = make();
  const started = Date.now();
  const pending = engine().trailingDigest({ transport: t });
  await sleep(10); close(); t.abort('closed', 'test');
  await assert.rejects(pending, (e) => e.kind === 'closed');
  assert.ok(Date.now() - started < 2500);
});

test('a frame that is not 16 bytes is partial or invalid, not a digest', async () => {
  for (const [bytes, state] of [[new Uint8Array(9).fill(5), 'partial'], [new Uint8Array(20).fill(5), 'invalid']]) {
    const { t } = make();
    feed(t, frame(bytes));
    assert.equal((await engine().trailingDigest({ transport: t })).state, state);
  }
});

test('line noise before the frame is invalid-packet (the stream is then not trusted), never a digest', async () => {
  const { t } = make();
  feed(t, Uint8Array.from([0x55, ...frame(MD5)]));
  assert.equal((await engine().trailingDigest({ transport: t })).state, 'invalid');
});

test('WITHOUT the option the frame is simply discarded by the next command (flushInput), as before: nothing in the bench reads it', async () => {
  const { t } = make();
  feed(t, frame(MD5));
  t.buffer = new Uint8Array(0);                       // esptool-js: this.transport.flushInput() at the start of every command()
  feed(t, frame([1, 0x13, 2, 0, 0, 0, 0, 0, 0, 0]));
  const next = await t.read(500);
  assert.deepEqual([...next.slice(0, 2)], [1, 0x13]);
});

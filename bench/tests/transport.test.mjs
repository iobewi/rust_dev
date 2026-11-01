import test from "node:test";
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { makeBenchTransport } from "../web/bench-transport.js";
import { md5hex } from "../web/md5.js";
import { ESPError, FakeTransport, feed, response } from "./fakes.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const make = (env) => {
  const { BenchTransport, SerialFault } = makeBenchTransport(FakeTransport, ESPError, env);
  let end; const device = { pumpDone: new Promise((r) => { end = r; }) };
  return { t: new BenchTransport(device), SerialFault, closePort: end };
};

test("md5 matches node:crypto for the padding edge sizes", () => {
  for (const n of [0, 1, 55, 56, 63, 64, 65, 119, 120, 1000, 100003]) {
    const d = randomBytes(n);
    assert.equal(md5hex(d), createHash("md5").update(d).digest("hex"), "size " + n);
  }
});

test("1. a delayed FLASH_DEFL_BEGIN acknowledgement is received (woken by the data, not by a poll)", async () => {
  const { t } = make();
  setTimeout(() => feed(t, response(0x10)), 120);
  const t0 = performance.now();
  const p = await t.read(1000);
  assert.deepEqual([...p.slice(0, 2)], [1, 0x10]);
  assert.ok(performance.now() - t0 < 600);
});

test("2. an answer just before the limit is accepted", async () => {
  const { t } = make();
  setTimeout(() => feed(t, response(0x10)), 150);
  const p = await t.read(200);
  assert.equal(p[1], 0x10);
});

test("3. an answer already in the buffer at expiry is not lost (late timer, no wake-up)", async () => {
  // Clock and timers are controlled: the page is "in the background", its timer fires after the deadline.
  let clock = 0; const timers = [];
  const { t } = make({ now: () => clock, setTimeout: (f) => { timers.push(f); return timers.length; }, clearTimeout: () => {} });
  const reading = t.read(3000);
  await sleep(5);
  clock = 4200;                       // deadline passed, timer not yet fired
  t._buf = response(0x10);            // bytes are there but nothing woke the reader (the old failure mode)
  timers.forEach((f) => f());
  const p = await reading;
  assert.equal(p[1], 0x10);
});

test("   with no answer at all, a late timer gives a no-response fault after the deadline, never before", async () => {
  let clock = 0; const timers = [];
  const { t, SerialFault } = make({ now: () => clock, setTimeout: (f) => { timers.push(f); return timers.length; }, clearTimeout: () => {} });
  const reading = t.read(3000).then(() => "ok", (e) => e);
  await sleep(5);
  clock = 2000; timers.splice(0).forEach((f) => f()); await sleep(5);   // early wake-up: must keep waiting
  assert.equal(t.stats.timeouts, 0);
  clock = 3001; timers.splice(0).forEach((f) => f());
  const e = await reading;
  assert.ok(e instanceof SerialFault);
  assert.equal(e.kind, "no-response");
  assert.match(e.message, /aucun octet reçu en 3000 ms/);
  assert.doesNotMatch(e.message, /noise|corruption/);
});

test("4. a port closed during a command ends the read as `closed`, not as a timeout", async () => {
  const { t, SerialFault } = make();
  const reading = t.read(5000).then(() => "ok", (e) => e);
  setTimeout(() => t.abort("closed", "USB débranché"), 30);
  const e = await reading;
  assert.ok(e instanceof SerialFault);
  assert.equal(e.kind, "closed");
  assert.match(e.message, /USB débranché/);
  await assert.rejects(t.read(50), (x) => x.kind === "closed");   // and stays closed
});

test("   the end of the read stream closes the transport", async () => {
  const { t, closePort } = make();
  const loop = t.readLoop();
  const reading = t.read(5000).then(() => "ok", (e) => e);
  closePort(); await loop;
  assert.equal((await reading).kind, "closed");
});

test("   noise is reported as invalid-packet, a cut packet as partial-packet", async () => {
  const a = make(); feed(a.t, Uint8Array.from([0x41, 0x42]));
  await assert.rejects(a.t.read(50), (e) => e.kind === "invalid-packet");
  const b = make(); feed(b.t, Uint8Array.from([0xc0, 1, 2]));
  await assert.rejects(b.t.read(40), (e) => e.kind === "partial-packet");
});

test("   a packet split across chunks and trailing bytes stay in order", async () => {
  const { t } = make();
  const two = Uint8Array.from([...response(1), ...response(2)]);
  feed(t, two.slice(0, 5));
  setTimeout(() => feed(t, two.slice(5)), 20);
  assert.equal((await t.read(500))[1], 1);
  assert.equal((await t.read(500))[1], 2);
});

test("   read statistics and events carry durations, not payloads", async () => {
  const { t } = make(); const seen = [];
  t.onEvent = (n, d) => seen.push([n, d]);
  setTimeout(() => feed(t, response(3)), 20);
  await t.read(500);
  assert.ok(t.stats.reads === 1 && t.stats.bytesIn > 0 && t.stats.readMs >= 0);
  assert.deepEqual(seen.map((s) => s[0]), ["read-start", "first-byte", "read-end"]);
  assert.ok(!JSON.stringify(seen).includes("c0010"));
});

test("   a baud-rate change (disconnect, connect, new readLoop) leaves a working session, whatever order the old loop ends in", async () => {
  // esptool-js: changeBaudrate() = disconnect(); connect(newBaud); readLoop(). Reads that must wait would otherwise
  // fail as `closed` for the rest of the session (seen in Firefox with the real library, not with a stub).
  const { BenchTransport } = makeBenchTransport(FakeTransport, ESPError);
  let end1; const device = { pumpDone: new Promise((r) => { end1 = r; }) };
  const t = new BenchTransport(device);
  const loop1 = t.readLoop();
  await t.disconnect();
  await t.connect(460800);
  let end2; device.pumpDone = new Promise((r) => { end2 = r; });
  const loop2 = t.readLoop();
  end1(); await loop1;                       // the old loop ends AFTER the reopen
  setTimeout(() => feed(t, response(0x10)), 80);
  const p = await t.read(1000);              // must wait for the data, not throw `closed`
  assert.equal(p[1], 0x10);
  end2(); await loop2;
  await assert.rejects(() => t.read(50), (e) => e.kind === "closed");   // a real end of the new session still closes it
});

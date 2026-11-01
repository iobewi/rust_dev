import test from "node:test";
import assert from "node:assert/strict";
import { runWrite, Tracer } from "../web/flash-op.js";
import { md5hex } from "../web/md5.js";
import { ESPError, fakeLoader } from "./fakes.mjs";

const item = (addr, n = 64) => ({ path: "p91/x.bin", addr, sha: "00", data: Uint8Array.from({ length: n }, (_, i) => i) });
const fault = (kind, msg) => Object.assign(new ESPError(msg), { kind });
const count = (l, n) => l.calls.filter((c) => c[0] === n).length;

test("success: transfer complete, md5 verified, read-back explicitly not run", async () => {
  const it = item(0x1000);
  const l = fakeLoader({ flashMd5sum: () => md5hex(it.data) });
  const r = await runWrite(l, [it]);
  assert.equal(r.ok, true);
  assert.equal(r.state, "completed");
  assert.deepEqual(r.verification, { transfer: "complete", md5: "verified", sha256_readback: "not-run" });
  assert.equal(r.requires_check, false);
});

test("md5 can be switched off: ok, but md5 is `not-run`", async () => {
  const r = await runWrite(fakeLoader(), [item(0x1000)], { md5: false });
  assert.equal(r.ok, true);
  assert.equal(r.verification.md5, "not-run");
});

test("an error before anything was sent is `failed`, flash unchanged", async () => {
  const l = fakeLoader(); l.writeFlash = async () => { throw new Error("File 1 doesn't fit"); };
  const r = await runWrite(l, [item(0)]);
  assert.deepEqual([r.ok, r.state, r.flash, r.requires_check], [false, "failed", "unchanged", false]);
});

test("10/ a begin that gets no answer is INDETERMINATE (erase may have happened), and is not retried", async () => {
  const l = fakeLoader({ flashDeflBegin: () => { throw fault("no-response", "réponse absente : aucun octet reçu en 3000 ms"); } });
  const r = await runWrite(l, [item(0)]);
  assert.equal(r.ok, false);
  assert.equal(r.state, "indeterminate");
  assert.equal(r.flash, "possibly-erased");
  assert.equal(r.failed_at, "begin");
  assert.equal(r.requires_check, true);
  assert.equal(r.hold, true);
  assert.equal(r.error_kind, "no-response");
  assert.equal(count(l, "flashDeflBegin"), 1);        // no blind retry
  assert.deepEqual(r.regions.map((x) => [x.addr, x.begin]), [["0x0", "sent"]]);
});

test("interruption after the erase, before the data is programmed: indeterminate", async () => {
  const l = fakeLoader({ flashDeflBlock: () => { throw fault("closed", "port série fermé"); } });
  const r = await runWrite(l, [item(0x10000)]);
  assert.equal(r.state, "indeterminate");
  assert.equal(r.failed_at, "data");
  assert.equal(r.verification.transfer, "incomplete");
  assert.equal(r.regions[0].begin, "acknowledged");
});

test("a port closed during the command is indeterminate, never a success", async () => {
  const l = fakeLoader({ flashDeflFinish: () => { throw fault("closed", "x"); } });
  const r = await runWrite(l, [item(0)]);
  assert.deepEqual([r.ok, r.state], [false, "indeterminate"]);
});

test("md5 mismatch: failed, flash content known to differ, check required", async () => {
  const l = fakeLoader({ flashMd5sum: () => "00".repeat(16) });
  const r = await runWrite(l, [item(0)]);
  assert.deepEqual([r.ok, r.state, r.flash, r.verification.md5, r.requires_check], [false, "failed", "content-mismatch", "mismatch", true]);
});

test("md5 check that gets no answer: transfer complete but unverified = indeterminate", async () => {
  const l = fakeLoader({ flashMd5sum: () => { throw fault("no-response", "x"); } });
  const r = await runWrite(l, [item(0)]);
  assert.deepEqual([r.state, r.flash, r.verification.transfer, r.verification.md5], ["indeterminate", "written-unverified", "complete", "indeterminate"]);
});

test("a failure on the second image reports the first as complete", async () => {
  let n = 0;
  const l = fakeLoader({ flashDeflBegin: () => { if (++n === 2) throw fault("no-response", "x"); }, flashMd5sum: (a, s) => md5hex(Uint8Array.from({ length: s }, (_, i) => i)) });
  const r = await runWrite(l, [item(0x1000), item(0x2000)]);
  assert.equal(r.state, "indeterminate");
  assert.deepEqual(r.regions.map((x) => [x.addr, x.transfer]), [["0x1000", "complete"], ["0x2000", "pending"]]);
});

test("a chip erase that fails is indeterminate", async () => {
  const l = fakeLoader({ eraseFlash: () => { throw fault("no-response", "x"); } });
  const r = await runWrite(l, [item(0)], { erase: true });
  assert.deepEqual([r.state, r.flash], ["indeterminate", "possibly-erased"]);
});

test("trace: monotonic timestamps, visibility, begin start/end, no payload", async () => {
  let v = "visible", t = 100;
  const tr = new Tracer({ now: () => (t += 1.5), visibility: () => v });
  const l = fakeLoader({ flashDeflBegin: () => { v = "hidden"; } });
  await runWrite(l, [item(0)], { tracer: tr, md5: false });
  const names = tr.snapshot().map((e) => e.ev);
  assert.ok(names.includes("flashDeflBegin-start") && names.includes("flashDeflBegin-end"));
  const ts = tr.snapshot().map((e) => e.t);
  assert.deepEqual(ts, [...ts].sort((a, b) => a - b));
  assert.ok(tr.snapshot().some((e) => e.vis === "hidden"));
  assert.ok(!JSON.stringify(tr.snapshot()).includes('"data"'));
});

import test from "node:test";
import assert from "node:assert/strict";
import { makePoller } from "../web/poller.js";

const fakeTimers = () => {
  const q = []; let id = 0;
  return { q, env: { setTimeout: (f, ms) => { q.push({ id: ++id, f, ms }); return id; }, clearTimeout: (i) => { const k = q.findIndex((t) => t.id === i); if (k >= 0) q.splice(k, 1); } },
           fire: async () => { const t = q.shift(); if (t) await t.f(); await new Promise((r) => setImmediate(r)); } };
};

test("11/ a slow inventory never overlaps itself, whatever the number of triggers", async () => {
  const T = fakeTimers(); let active = 0, max = 0, runs = 0, release;
  const p = makePoller(async () => { runs++; active++; max = Math.max(max, active); await new Promise((r) => { release = r; }); active--; }, 3000, T.env);
  p.start(); p.trigger(); p.trigger(); p.trigger();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual([runs, active], [1, 1]);
  release(); await new Promise((r) => setImmediate(r));          // finishes; one coalesced re-run starts
  assert.equal(runs, 2);
  release(); await new Promise((r) => setImmediate(r));
  assert.equal(max, 1);
  assert.equal(T.q.length, 1);                                   // next run scheduled after the end
  assert.equal(T.q[0].ms, 3000);
});

test("held during a flash operation: no request, nothing lost afterwards", async () => {
  const T = fakeTimers(); let runs = 0, busy = true;
  const p = makePoller(async () => { runs++; }, 3000, T.env);
  p.hold(() => busy);
  p.start(); await new Promise((r) => setImmediate(r));
  assert.equal(runs, 0);
  await T.fire(); assert.equal(runs, 0);                          // still busy at the next tick
  busy = false; await T.fire(); assert.equal(runs, 1);
});

test("a failing task does not stop the polling", async () => {
  const T = fakeTimers(); let runs = 0;
  const p = makePoller(async () => { runs++; throw new Error("injoignable"); }, 3000, T.env);
  p.start(); await new Promise((r) => setImmediate(r));
  await T.fire(); assert.equal(runs, 2);
});

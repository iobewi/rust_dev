// The page's command client against the real Python server over HTTP (no board, no browser).
import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { makeClient } from "../web/cmd-client.js";
import { Tracer } from "../web/flash-op.js";

let proc, base, dir;
const free = () => new Promise((res) => { const s = createServer().listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => res(p)); }); });
const j = async (path, body) => (await fetch(base + path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const client = (page, execute) => makeClient({ page, execute, busy: () => false, bench: () => {}, tracer: new Tracer({ visibility: () => "visible" }), sleep,
  fetchJson: async (path, body) => { const r = await fetch(base + path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const x = await r.json().catch(() => ({})); return body === undefined ? x : { ...x, ok: r.ok && x.ok !== false }; } });

before(async () => {
  dir = mkdtempSync(join(tmpdir(), "bench-e2e-"));
  const port = await free(); base = `http://127.0.0.1:${port}`;
  proc = spawn("python3", [new URL("../server/bench_server.py", import.meta.url).pathname], { env: { ...process.env, BENCH_PORT: String(port), BENCH_WEB: dir, BENCH_LOGS: dir }, stdio: "ignore" });
  for (let i = 0; i < 50; i++) { try { await j("/api/queue"); return; } catch (e) { await sleep(100); } }
  throw new Error("server did not start");
});
after(() => { proc.kill(); rmSync(dir, { recursive: true, force: true }); });

test("nominal: the command runs once and its outcome is on the server", async () => {
  await j("/api/cmd", { op: "mark", req: "n1" });
  const ran = []; const c = client("A", async (cmd) => { ran.push(cmd.req); return { ok: true }; });
  assert.equal(await c.cycle(), "ran");
  assert.deepEqual(ran, ["n1"]);
  assert.equal((await j("/api/result?req=n1")).state, "completed");
  assert.equal(await c.cycle.call(null) , "idle");     // nothing left, nothing replayed
});

test("6/ reload during a write: indeterminate for the sender, the late result changes nothing, nothing is replayed", async () => {
  await j("/api/cmd", { op: "write", req: "w1" });
  let release; const gate = new Promise((r) => { release = r; });
  const ranA = []; const a = client("OLD", async (cmd) => { ranA.push(cmd.req); await gate; return { ok: true, state: "completed" }; });
  const running = a.cycle();
  await sleep(300);
  assert.equal((await j("/api/status?req=w1")).state, "running");
  await j("/api/cmd", { op: "dump", req: "d1" });
  const ranB = []; const b = client("NEW", async (cmd) => { ranB.push(cmd.req); return { ok: true }; });   // the reloaded page
  assert.equal(await b.cycle(), "ran");
  assert.deepEqual(ranB, ["d1"]);                                       // w1 is not given again
  const st = await j("/api/status?req=w1");
  assert.deepEqual([st.state, st.executed], ["indeterminate", "started"]);
  release(); await running;                                             // the old page finishes and reports
  assert.equal((await j("/api/result?req=w1")).state, "indeterminate"); // the verdict stands
  assert.equal((await j("/api/status?req=w1")).late_result.state, "completed");
  assert.deepEqual(ranA, ["w1"]); assert.deepEqual(ranB, ["d1"]);
});

test("8/ a command that waited too long is not executed when the page finally polls", async () => {
  await j("/api/cmd", { op: "write", req: "x1", ttl: 0.2 });
  await sleep(400);
  const ran = []; const c = client("C", async (cmd) => { ran.push(cmd.req); return { ok: true }; });
  assert.equal(await c.cycle(), "idle");
  assert.deepEqual(ran, []);
  const r = await j("/api/result?req=x1");
  assert.deepEqual([r.state, r.executed], ["expired", false]);
});

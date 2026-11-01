import test from "node:test";
import assert from "node:assert/strict";
import { makeClient, consoleGuard, normalize } from "../web/cmd-client.js";
import { Tracer } from "../web/flash-op.js";

// A scripted server: queue of commands to hand out, record of what the page posted.
function rig({ cmds = [], ackReply = { ok: true }, failPost = () => false, execute, busy = () => false } = {}) {
  const posted = [], bench = [], executed = [];
  const fetchJson = async (path, body) => {
    if (body === undefined) return cmds.shift() || {};
    if (failPost(path)) throw new Error("réseau");
    posted.push([path, body]);
    if (path === "/api/ack") return ackReply;
    return { ok: true };
  };
  const c = makeClient({ page: "P1", fetchJson, execute: async (cmd) => { executed.push(cmd.req); return execute ? execute(cmd) : { ok: true }; },
    busy, bench: (m) => bench.push(m), tracer: new Tracer({ visibility: () => "visible" }), sleep: async () => {} });
  return { c, posted, bench, executed };
}
const cmd = (req, op = "write") => ({ op, req, claim: "k" + req });

test("nominal: poll, ack, execute, result with the claim", async () => {
  const r = rig({ cmds: [cmd("a")] });
  assert.equal(await r.c.cycle(), "ran");
  assert.deepEqual(r.posted.map((p) => p[0]), ["/api/ack", "/api/result"]);
  assert.equal(r.posted[1][1].claim, "ka");
  assert.equal(r.posted[1][1].state, "completed");
});

test("no acknowledgement (network): the command is NOT executed", async () => {
  const r = rig({ cmds: [cmd("a")], failPost: (p) => p === "/api/ack" });
  assert.equal(await r.c.cycle(), "skipped");
  assert.deepEqual(r.executed, []);
});

test("8/ acknowledgement refused (expired or already started): NOT executed", async () => {
  const r = rig({ cmds: [cmd("a")], ackReply: { ok: false, state: "expired" } });
  assert.equal(await r.c.cycle(), "skipped");
  assert.deepEqual(r.executed, []);
  assert.match(r.bench.join("\n"), /NOT executed/);
});

test("7/ a button operation started meanwhile: the command is released, not started", async () => {
  const r = rig({ cmds: [cmd("a")], busy: () => true });
  assert.equal(await r.c.cycle(), "skipped");
  assert.deepEqual(r.posted.map((p) => p[0]), ["/api/release"]);
  assert.deepEqual(r.executed, []);
});

test("9/ a result the server cannot take now is kept and delivered at a later cycle", async () => {
  let down = true;
  const r = rig({ cmds: [cmd("a")], failPost: (p) => down && p === "/api/result" });
  await r.c.cycle();
  assert.equal(r.c.pending.length, 1);                    // kept, not lost
  down = false;
  await r.c.cycle();                                      // next cycle: idle poll, but the result goes first
  assert.equal(r.c.pending.length, 0);
  assert.equal(r.posted.filter((p) => p[0] === "/api/result")[0][1].req, "a");
});

test("a failing executor gives `failed`, an indeterminate one stays indeterminate (never a success)", async () => {
  const a = rig({ cmds: [cmd("a")], execute: () => { throw new Error("boom"); } });
  await a.c.cycle();
  assert.deepEqual([a.posted[1][1].state, a.posted[1][1].ok], ["failed", false]);
  const b = rig({ cmds: [cmd("b")], execute: () => ({ ok: false, state: "indeterminate", flash: "possibly-erased" }) });
  await b.c.cycle();
  assert.deepEqual([b.posted[1][1].state, b.posted[1][1].ok, b.posted[1][1].flash], ["indeterminate", false, "possibly-erased"]);
  assert.deepEqual(normalize({ ok: true, state: "indeterminate" }), { ok: false, state: "indeterminate" });
  assert.equal(normalize(undefined).state, "failed");
});

test("the executor is not tied to the HTTP side: a slow operation runs to its end while polls would fail", async () => {
  let release; const gate = new Promise((r) => { release = r; });
  const r = rig({ cmds: [cmd("a")], execute: async () => { await gate; return { ok: true }; } });
  const running = r.c.cycle();
  await new Promise((x) => setTimeout(x, 20));
  assert.deepEqual(r.posted.map((p) => p[0]), ["/api/ack"]);   // started, still running, nothing aborted
  release();
  assert.equal(await running, "ran");
});

test("12/ reset, console, tx and io0 are refused during a flash operation and after an unknown outcome", () => {
  for (const op of ["reset", "console", "tx", "io0"]) {
    assert.match(consoleGuard({ op }, { opRunning: true, mode: "busy", indet: null }), /en cours/);
    assert.match(consoleGuard({ op }, { opRunning: false, mode: "loader", indet: { label: "écriture" } }), /indéterminé/);
    assert.equal(consoleGuard({ op }, { opRunning: false, mode: "console", indet: null }), null);
  }
  assert.equal(consoleGuard({ op: "dump" }, { opRunning: false, mode: "loader", indet: { label: "x" } }), null);   // read-back to verify stays possible
  assert.equal(consoleGuard({ op: "write" }, { opRunning: false, mode: "loader", indet: { label: "x" } }), null);  // so does a restore
});

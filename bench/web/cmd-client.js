// Page side of the command lifecycle (see server/commands.py). One cycle: poll -> acknowledge -> execute -> deliver.
//
// - A command is executed only after the server accepted its acknowledgement: no ack, no execution. The server
//   decides about expiry, the page does not compare clocks.
// - The result is kept until the server has it: a lost HTTP connection never loses a result nor stops the work.
// - Nothing here aborts a running operation. If the page has no answer to give, it gives `failed`, or
//   `indeterminate` when the executor says so.
export function normalize(r) {
  if (!r || typeof r !== "object") return { ok: false, state: "failed", error: "pas de résultat" };
  const state = ["completed", "failed", "indeterminate"].includes(r.state) ? r.state : (r.ok ? "completed" : "failed");
  return { ...r, state, ok: !!r.ok && state === "completed" };
}

export const CONSOLE_OPS = new Set(["reset", "console", "tx", "io0"]);
/** Why a command that acts on the port lines (or reopens it: a reopen resets the board) must not run now, or null. */
export function consoleGuard(cmd, st) {
  if (!CONSOLE_OPS.has(cmd.op)) return null;
  if (st.opRunning || st.mode === "busy") return cmd.op + " refusé : opération flash ou relecture en cours";
  if (st.indet) return cmd.op + " refusé : état indéterminé (" + st.indet.label + "), à lever par l'opérateur depuis la page";
  return null;
}

export function makeClient(d) {
  // d: { page, fetchJson(path, body?), execute(cmd), busy(), bench(msg), tracer, sleep(ms) }
  const pending = [];            // results the server has not acknowledged yet
  const post = (path, body) => d.fetchJson(path, body);

  async function deliver(cmd, r) {
    pending.push({ req: cmd.req, body: { req: cmd.req, op: cmd.op, claim: cmd.claim, ...r } });
    await flush();
  }
  async function flush(attempts = 3) {
    while (pending.length) {
      const item = pending[0];
      let sent = false;
      for (let i = 0; i < attempts && !sent; i++) {
        try { await post("/api/result", item.body); sent = true; }
        catch (e) { if (i + 1 < attempts) await d.sleep(500 * (i + 1)); }
      }
      if (!sent) { d.bench(`result of ${item.req} not delivered yet (${pending.length} kept, retried at the next cycle)`); return; }
      pending.shift();
    }
  }

  /** One cycle. Returns "idle" | "ran" | "skipped". Throws only when the server cannot be reached. */
  async function cycle() {
    await flush(1);
    const cmd = await d.fetchJson(`/api/cmd?wait=25&page=${d.page}`);
    if (!cmd || !cmd.op) return "idle";
    if (d.busy()) {                               // a button started an operation meanwhile: not started, back in the queue
      await post("/api/release", { req: cmd.req, claim: cmd.claim }).catch(() => {});
      d.bench(`command ${cmd.op} ${cmd.req} released: operation in progress`);
      return "skipped";
    }
    let ack;
    try { ack = await post("/api/ack", { req: cmd.req, claim: cmd.claim, page: d.page }); }
    catch (e) { d.bench(`command ${cmd.op} ${cmd.req}: no acknowledgement, NOT executed (${e.message})`); return "skipped"; }
    if (!ack || ack.ok === false) { d.bench(`command ${cmd.op} ${cmd.req} refused by the server (${ack && ack.state}), NOT executed`); return "skipped"; }
    d.tracer.ev("cmd-start", { req: cmd.req, op: cmd.op });
    const t0 = d.tracer.now();
    let r;
    try { r = normalize(await d.execute(cmd)); }
    catch (e) { r = { ok: false, state: "failed", error: (e && e.message) || String(e) }; }
    d.tracer.ev("cmd-end", { req: cmd.req, op: cmd.op, state: r.state, ms: +(d.tracer.now() - t0).toFixed(3) });
    await deliver(cmd, r);
    return "ran";
  }
  return { cycle, flush, pending };
}

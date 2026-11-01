// Mission 3: critical scenarios replayed in Firefox (real page, real esptool-js 0.7.0, simulated board, no hardware).
import fs from "node:fs";
import { Q, DIR, check, info, j, enq, waitState, waitFor, sleep, startServer, launch, newPage, boot, sha256, shot, capture, events, results, saveResults, result, shaOf, status } from "./lib.mjs";

const only = process.argv[2];
const browser = await launch();
const wr = async (paths, extra = {}) => {
  const items = []; for (const [p, addr] of paths) items.push({ path: p, addr, sha: await shaOf(p) });
  return enq("write", { items, ...extra });
};
const FILE = (p) => fs.readFileSync(DIR + "/web/" + p);
const simState = (P) => P.sim(() => ({ opens: __sim.opens, closes: __sim.closes, sig: __sim.sig.length, begins: __sim.cmds.filter((c) => c.ev === "cmd" && c.op === 0x10).length, mode: __sim.mode }));
const cap0 = () => capture().length;
const capSince = (n) => capture().slice(n);
async function fresh(S, env) {
  await startServer(env);
  const P = await newPage(browser, "A");
  await boot(P);
  await P.sim(() => { __sim.preload(0, new Uint8Array(6000).fill(0xaa)); __sim.preload(0x10000, new Uint8Array(40000).fill(0xaa)); });   // known previous content
  return P;
}
const settle = async (P) => { await waitFor(async () => /console|chargeur/.test(await P.text("#mode")), 15000); };
const never = (S, text, why) => check(S, "no success announced: " + why, !/written, md5 verified|written, md5 not run/.test(text));

const scenarios = {
  // ------------------------------------------------------------------ control: a normal write is proven, not assumed
  async control() {
    const S = "C0-write-ok";
    const P = await fresh(S); const c0 = cap0();
    const r = await wr([["p91/boot.bin", 0]]);
    const st = await waitState(r.req, ["completed", "failed", "indeterminate", "expired"], 40000);
    const res = await result(r.req);
    check(S, "write → completed", st.state === "completed", JSON.stringify(res).slice(0, 300));
    check(S, "verification: md5 verified, sha256_readback not-run", res.verification && res.verification.md5 === "verified" && res.verification.sha256_readback === "not-run", JSON.stringify(res.verification));
    check(S, "simulated flash = file (independent comparison)", sha256(Buffer.from(await P.sim(() => Array.from(__sim.flash.slice(0, 6000))))) === sha256(FILE("p91/boot.bin")));
    const md5Seen = (await P.sim(() => __sim.cmds.filter((c) => c.op === 0x13).length));
    check(S, "the loader asked for the MD5 of the region (SPI_FLASH_MD5 sent)", md5Seen >= 1, md5Seen);
    const cap = capSince(c0);
    check(S, "capture: 'written, md5 verified by the loader' after the MD5 command", /md5 verified by the loader/.test(cap));
    await settle(P);
    check(S, "no indeterminate pill", await P.page.locator("#indet").isHidden());
    // independent read-back as the campaign would do it
    const d = await enq("dump", { regions: [{ name: "q-c0", addr: 0, size: 6000 }] });
    await waitState(d.req, ["completed", "failed", "indeterminate"], 30000);
    check(S, "read-back SHA-256 (dump) = sha256 of the written file", sha256(fs.readFileSync(DIR + "/logs/dumps/q-c0.bin")) === sha256(FILE("p91/boot.bin")));
    await P.ctx.close();
  },

  // ------------------------------------------------------------------ 1 + 7 + reconnection + reload
  async delayed_begin() {
    const S = "C1-begin-ack-delayed";
    const P = await fresh(S); const c0 = cap0();
    await P.sim(() => { __sim.cfg.beginDelayMs = 3600; });   // acknowledgement after the 3000 ms limit of the loader
    const s0 = await simState(P);
    const r = await wr([["p91/boot.bin", 0]]);
    const st = await waitState(r.req, ["completed", "failed", "indeterminate", "expired"], 40000);
    const res = await result(r.req);
    check(S, "late FLASH_DEFL_BEGIN ack → indeterminate (not failed, not completed)", st.state === "indeterminate" && res.ok === false, st.state + " " + JSON.stringify(res).slice(0, 240));
    check(S, "flash status 'possibly-erased', requires_check, hold", res.flash === "possibly-erased" && res.requires_check === true && res.hold === true, `${res.flash} ${res.requires_check} ${res.hold}`);
    check(S, "error kind = no-response (timeout), stage begin", res.error_kind === "no-response" && /begin/.test(String(res.failed_at)), `${res.error_kind} / ${res.failed_at}`);
    await sleep(2500);   // the stub's late acknowledgement now arrives
    const s1 = await simState(P);
    check(S, "FLASH_DEFL_BEGIN sent exactly once (never retried)", s1.begins - s0.begins === 1, s1.begins - s0.begins);
    check(S, "the stub did erase before acknowledging (flash region is 0xFF): the indeterminate verdict was warranted", (await P.sim(() => __sim.flash.slice(0, 6000).every((b) => b === 0xff))));
    const st2 = await status(r.req);
    check(S, "late acknowledgement does not turn the verdict into success", st2.state === "indeterminate");
    const cap = capSince(c0);
    never(S, cap, "capture has no 'written' line");
    check(S, "capture carries INDETERMINATE and a [trace] dump", /INDETERMINATE/.test(cap) && /\[trace\]/.test(cap));
    check(S, "UI: pill 'état indéterminé' visible with its clear button", (await P.page.locator("#indet").isVisible()) && (await P.page.locator("#indetclear").isVisible()), await P.text("#indet"));
    check(S, "mode shows the loader (no automatic console return)", /chargeur/.test(await P.text("#mode")), await P.text("#mode"));
    const reopenedAt115200 = await P.sim(() => { const i = __sim.cmds.findIndex((c) => c.ev === "begin"); return __sim.cmds.slice(i).some((c) => c.ev === "open" && c.baud === 115200); });
    check(S, "no console reopen (no 115200 open, so no board reset) after the begin; board still in ROM download mode", !reopenedAt115200 && s1.mode === "rom", JSON.stringify([s0, s1]));
    await shot(P, "C1-indeterminate");

    // 7. dangerous actions after indeterminate
    const refused = {};
    for (const [op, extra] of [["reset", {}], ["console", {}], ["tx", { hex: "00" }], ["io0", { ms: 100 }], ["arm", { pattern: "x" }]]) {
      const q = await enq(op, extra); const s = await waitState(q.req, ["completed", "failed", "indeterminate", "expired"], 20000); const rr = await result(q.req);
      refused[op] = `${s.state}:${(rr.error || "").slice(0, 70)}`;
    }
    info(S, "remote commands after indeterminate: " + JSON.stringify(refused));
    check(S, "reset / console / tx / io0 refused remotely (state failed)", ["reset", "console", "tx", "io0"].every((o) => refused[o].startsWith("failed")), JSON.stringify(refused));
    const s2 = await simState(P);
    check(S, "no EN/IO0 signal and no port reopen caused by those commands", s2.sig === s1.sig && s2.opens === s1.opens, JSON.stringify([s1, s2]));
    // UI: Retour console with the confirmation dismissed, then accepted
    P.dialogMode = "dismiss"; await P.page.evaluate(() => document.getElementById("toconsole").click());
    await sleep(500);
    const s3 = await simState(P);
    check(S, "'Retour console' needs a confirmation; dismissed → nothing happens", s3.opens === s2.opens && (await P.page.locator("#indet").isVisible()), JSON.stringify(P.dialogs.slice(-1)));
    // Reset button (operator) while in loader mode
    await P.page.evaluate(() => document.getElementById("enreset").onclick());
    await sleep(300);
    check(S, "'Reset EN' handler called by force: refused outside console mode", (await simState(P)).sig === s2.sig);

    // reconnection: USB glitch must not clear or bypass the state, nor reboot the board by itself
    await P.sim(() => __sim.port.vanish()); await sleep(600);
    check(S, "after USB loss the indeterminate pill is still shown", await P.page.locator("#indet").isVisible());
    const before = await simState(P);
    await P.page.evaluate(() => { const ev = new Event("connect"); Object.defineProperty(ev, "target", { value: __sim.port }); navigator.serial.dispatchEvent(ev); });
    await sleep(1500);
    const after = await simState(P);
    const reopened = after.opens > before.opens;
    check(S, "USB re-appearance does NOT reopen the console (that would reset the board before it is verified)", !reopened, `opens ${before.opens} → ${after.opens}, mode ${await P.text("#mode")}`);
    check(S, "the indeterminate state is NOT cleared by the reconnection", await P.page.locator("#indet").isVisible());
    await shot(P, "C1-after-reconnect");

    // reload: the lock must survive
    await P.page.reload({ waitUntil: "load" }); await sleep(800);
    const pill = await P.page.locator("#indet").isVisible();
    check(S, "indeterminate state survives a page reload", pill, "pill visible after reload = " + pill);
    await P.ctx.close();
  },

  // ------------------------------------------------------------------ 2. answer present before the deadline, handled late (throttled timers)
  async late_handling() {
    const S = "C2-throttled-timers";
    const P = await fresh(S);
    await P.sim(() => { __sim.throttle = true; __sim.throttleStep = 4000; __sim.cfg.beginDelayMs = 2900; });
    const t0 = Date.now();
    const r = await wr([["p91/boot.bin", 0]]);
    const st = await waitState(r.req, ["completed", "failed", "indeterminate", "expired"], 60000);
    const res = await result(r.req);
    check(S, "begin acknowledged at 2.9 s (limit 3 s) with timers waking only every 4 s → completed", st.state === "completed" && res.ok === true, st.state + " " + (res.error || ""));
    check(S, "flash = file", sha256(Buffer.from(await P.sim(() => Array.from(__sim.flash.slice(0, 6000))))) === sha256(FILE("p91/boot.bin")));
    info(S, `took ${Date.now() - t0} ms (timers on a 4 s grid)`);
    // answer arrives after the nominal 3 s but before the throttled timer fires (4 s)
    await P.sim(() => { __sim.cfg.beginDelayMs = 3500; });
    const r2 = await wr([["p91/boot.bin", 0]]);
    const st2 = await waitState(r2.req, ["completed", "failed", "indeterminate", "expired"], 60000);
    const res2 = await result(r2.req);
    info(S, "answer at 3.5 s, timer wakes at 4 s: " + st2.state + " " + (res2.error || ""));
    check(S, "an answer that is already there when the late timer fires is used (verdict completed or indeterminate, never failed/unchanged)", ["completed", "indeterminate"].includes(st2.state), st2.state);
    await P.ctx.close();
  },

  // ------------------------------------------------------------------ 3. transport closed during an operation
  async transport_closed() {
    const S = "C3-transport-closed";
    for (const [label, cfg, cases] of [["during data block 2", { closeAtBlock: 2 }, "app"], ["right after begin", { closeOnBegin: true }, "boot"]]) {
      const P = await fresh(S); const c0 = cap0();
      await P.sim((c) => Object.assign(__sim.cfg, c), cfg);
      const r = await wr([cases === "app" ? ["p91/big.bin", 0x10000] : ["p91/boot.bin", 0]]);
      const st = await waitState(r.req, ["completed", "failed", "indeterminate", "expired"], 40000);
      const res = await result(r.req);
      check(S + " " + label, "verdict indeterminate, ok=false, delivered to the server although the port is gone", st.state === "indeterminate" && res.ok === false, st.state + " " + JSON.stringify(res).slice(0, 220));
      check(S + " " + label, "error_kind closed (or no-response) — never 'noise'", ["closed", "no-response", "partial-packet"].includes(res.error_kind) && !/noise or corruption/i.test(res.error || ""), `${res.error_kind}: ${res.error}`);
      never(S + " " + label, capSince(c0), "capture has no 'written' line");
      await sleep(300);
      check(S + " " + label, "indeterminate pill shown after the USB loss", await P.page.locator("#indet").isVisible(), await P.text("#mode"));
      const lo = cases === "app" ? 0x10000 : 0;
      const changed = await P.sim((lo) => __sim.flash.slice(lo, lo + 6000).some((b) => b !== 0xaa), lo);
      check(S + " " + label, "the region no longer holds its previous content (erase happened before the failure): 'indeterminate' was the right verdict", changed);
      await shot(P, "C3-" + cases);
      await P.ctx.close();
    }
  },

  // ------------------------------------------------------------------ 4. reload during a write + 6 reset during an operation + inventory held
  async reload_during_write() {
    const S = "C4-reload-during-write";
    const P = await fresh(S); const c0 = cap0();
    await P.sim(() => { __sim.cfg.blockDelayMs = 400; });
    const t0 = Date.now();
    const r = await wr([["p91/big.bin", 0x10000]]);
    const q = await enq("dump", { regions: [{ name: "q-c4-after", addr: 0x10000, size: 4096 }] });   // queued behind the write
    await waitFor(async () => (await P.sim(() => __sim.cmds.filter((c) => c.op === 0x11).length)) >= 3, 15000);
    // 6. reset attempts during the operation
    const sig0 = (await simState(P)).sig;
    const rs = await enq("reset");
    await P.page.evaluate(() => document.getElementById("enreset").onclick());
    await sleep(700);
    const stR = await status(rs.req);
    check(S, "remote reset during the write stays queued (one command in flight), not executed", stR.state === "queued", stR.state);
    check(S, "'Reset EN' pressed during the write: no EN pulse", (await simState(P)).sig === sig0, "sig " + sig0 + "→" + (await simState(P)).sig);
    check(S, "capture says the reset was refused", /reset EN (refusé|impossible)/.test(capSince(c0)), (capSince(c0).match(/reset EN[^\n]*/) || [""])[0]);
    // inventory requests must stop during the operation
    const n0 = P.requests.filter((x) => /\/api\/images/.test(x.url)).length; await sleep(5000);
    const n1 = P.requests.filter((x) => /\/api\/images/.test(x.url)).length;
    check(S, "no inventory request while the flash operation runs", n1 === n0, `${n0}→${n1}`);
    const begin = (await P.sim(() => __sim.cmds.filter((c) => c.op === 0x10).length));
    info(S, `operation running ${Date.now() - t0} ms, begin sent ${begin}x; reloading the page now`);
    const stBefore = await status(r.req);
    check(S, "server: command is Running when the page goes away", stBefore.state === "running", stBefore.state);
    await P.page.reload({ waitUntil: "load" });
    await sleep(300);
    const stDuring = await status(r.req);
    check(S, "a page reload does not change a Running command by itself", stDuring.state === "running" || stDuring.state === "indeterminate", stDuring.state);
    // new page instance: operator reconnects the port (authorization kept)
    await P.page.click("#connect");
    await waitFor(async () => (await P.text("#mode")) === "console", 15000);
    const st = await waitState(r.req, ["completed", "failed", "indeterminate", "expired"], 20000);
    const ev = events().filter((e) => e.req === r.req).map((e) => e.state + (e.reason ? ":" + e.reason : ""));
    check(S, "after the reload the old command is Indeterminate (started, page replaced) — not Completed, not replayed", st.state === "indeterminate" && st.executed === "started", JSON.stringify(st) + " " + ev.join(">"));
    info(S, "event trail: " + ev.join(" > "));
    const s = await simState(P);
    check(S, "the new page never re-sent FLASH_DEFL_BEGIN for that command", s.begins === 0, s.begins);
    // the command queued behind (read-only) may run now
    const stQ = await waitState(q.req, ["completed", "failed", "indeterminate", "expired"], 30000);
    info(S, "queued read-back behind the write: " + stQ.state);
    const stRs = await status(rs.req);
    info(S, "queued reset after the reload: " + stRs.state + " " + JSON.stringify((await result(rs.req)).error || ""));
    const pill = await P.page.locator("#indet").isVisible();
    check(S, "the new page still shows an indeterminate state for the interrupted write (lock survives the reload)", pill, "pill visible = " + pill);
    check(S, "the interrupted write did change the flash (previous content gone): the lock was warranted", await P.sim(() => __sim.flash.slice(0x10000, 0x10000 + 6000).some((b) => b !== 0xaa)) === true);
    await shot(P, "C4-after-reload");
    await P.ctx.close();
  },

  // ------------------------------------------------------------------ 5. late result (two page instances)
  async late_result() {
    const S = "C5-late-result";
    await startServer();
    const A = await newPage(browser, "A"); await boot(A);
    await A.sim(() => { __sim.cfg.blockDelayMs = 300; });
    const r = await wr([["p91/big.bin", 0x10000]]);
    await waitFor(async () => (await A.sim(() => __sim.cmds.filter((c) => c.op === 0x11).length)) >= 3, 15000);
    const B = await newPage(browser, "B"); await boot(B);                  // a second page instance starts polling
    const st = await waitState(r.req, ["indeterminate", "completed", "failed", "expired"], 15000);
    check(S, "second page instance takes over → the running command becomes Indeterminate immediately", st.state === "indeterminate", JSON.stringify(st));
    const done = await waitFor(async () => /md5 verified by the loader/.test(await A.log()), 40000);
    await sleep(800);
    const fin = await status(r.req);
    info(S, "final status: " + JSON.stringify(fin));
    check(S, "page A finished its write and posted its (late) result", !!done);
    check(S, "late result is kept as late_result and does not change the verdict", fin.state === "indeterminate" && fin.late_result && fin.late_result.state === "completed", JSON.stringify(fin.late_result));
    const res = await result(r.req);
    check(S, "GET /api/result still returns the synthetic indeterminate verdict", res.state === "indeterminate" && res.ok === false, JSON.stringify(res).slice(0, 200));
    const cap = capture();
    check(S, "the early verdict is not rewritten by the page's own success line (server ≠ page: server is conservative)", fin.state === "indeterminate");
    await A.ctx.close(); await B.ctx.close();
  },

  // ------------------------------------------------------------------ expired commands
  async expiry() {
    const S = "C6-expired-commands";
    await startServer({ ACK: "2" });
    const P = await newPage(browser, "A"); await boot(P);
    // a) queued while the page is busy long enough: ttl 3 s
    await P.sim(() => { __sim.cfg.blockDelayMs = 250; });
    const w = await wr([["p91/big.bin", 0x10000]]);
    await waitFor(async () => (await P.sim(() => __sim.cmds.filter((c) => c.op === 0x11).length)) >= 2, 15000);
    const ex = await enq("mark", { text: "must-not-run-late", ttl: 3 });
    await sleep(4500);
    const sx = await status(ex.req);
    check(S, "a command whose ttl ran out in the queue is Expired", sx.state === "expired" && sx.executed === false, JSON.stringify(sx));
    await waitState(w.req, ["completed", "failed", "indeterminate"], 40000);
    await sleep(1500);
    check(S, "once the page is free again the expired command is not executed late", !/must-not-run-late/.test(capture()));
    const sx2 = await status(ex.req);
    check(S, "its state stays Expired", sx2.state === "expired", sx2.state);
    // b) legacy expires field (absolute ms, in the past)
    const lg = await j("/api/cmd", { op: "mark", text: "past-expires", req: "legacy-" + Date.now(), expires: Date.now() - 5000 });
    await sleep(2500);
    check(S, "a command whose deadline is already past never runs", !/past-expires/.test(capture()), JSON.stringify(await status(lg.req)));
    // c) port not held: nothing is claimed
    await P.ctx.close();
    const q = await enq("mark", { text: "no-page", ttl: 2 });
    await sleep(3000);
    const sq = await status(q.req);
    check(S, "no page connected: the command waits then expires, not executed", sq.state === "expired" && !/no-page/.test(capture()), sq.state);
  },
};

const names = only && only !== "all" ? only.split(",") : Object.keys(scenarios);
for (const n of names) { try { await scenarios[n](); } catch (e) { check(n, "scenario ran to the end", false, e.stack.split("\n").slice(0, 3).join(" | ")); } }
await browser.close();
saveResults();
const bad = results.filter((x) => !x.pass).length;
console.log(`\n${results.length - bad}/${results.length} PASS`);

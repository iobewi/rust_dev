// Verdict display (failed / indeterminate), background tab, large inventory, stock-vs-bench Transport under throttled timers.
import fs from "node:fs";
import { Q, DIR, check, info, j, enq, waitState, waitFor, sleep, startServer, launch, newPage, boot, sha256, shot, capture, events, results, saveResults, result, shaOf, status } from "./lib.mjs";
const only = process.argv[2];
const browser = await launch();
const wr = async (paths, extra = {}) => { const items = []; for (const [p, addr] of paths) items.push({ path: p, addr, sha: await shaOf(p) }); return enq("write", { items, ...extra }); };
const fin = (req, ms = 40000) => waitState(req, ["completed", "failed", "indeterminate", "expired"], ms);
async function fresh() { await startServer(); const P = await newPage(browser, "A"); await boot(P); await P.sim(() => { __sim.preload(0, new Uint8Array(6000).fill(0xaa)); }); return P; }
const cap0 = () => capture().length;

const scenarios = {
  async verdicts() {
    const S = "V-verdict-display";
    // 1. failed, nothing sent: the loader never synchronises
    let P = await fresh(); let c0 = cap0();
    await P.sim(() => { __sim.cfg.noSync = true; });
    let r = await wr([["p91/boot.bin", 0]]); let st = await fin(r.req, 90000); let res = await result(r.req);
    check(S + " failed/unchanged", "loader never answers → state failed (nothing sent to the flash), not indeterminate", st.state === "failed" && res.ok === false, st.state + " " + JSON.stringify(res).slice(0, 200));
    check(S + " failed/unchanged", "no indeterminate pill, flash content untouched", (await P.page.locator("#indet").isHidden()) && (await P.sim(() => __sim.flash.slice(0, 6000).every((b) => b === 0xaa))));
    check(S + " failed/unchanged", "capture shows the error and no success", /ERREUR/.test(capture().slice(c0)) && !/md5 verified by the loader/.test(capture().slice(c0)), (capture().slice(c0).match(/ERREUR[^\n]*/) || [""])[0].slice(0, 160));
    await shot(P, "V-failed-unchanged"); await P.ctx.close();
    // 2. failed/content-mismatch: the chip's MD5 differs from the file's
    P = await fresh(); c0 = cap0();
    await P.sim(() => { __sim.cfg.md5Corrupt = true; });
    r = await wr([["p91/boot.bin", 0]]); st = await fin(r.req); res = await result(r.req);
    check(S + " md5 mismatch", "chip MD5 ≠ file MD5 → failed, flash=content-mismatch, never completed", st.state === "failed" && res.flash === "content-mismatch" && res.ok === false, st.state + " " + res.flash);
    check(S + " md5 mismatch", "requires_check → pill shown, no automatic console return", (await P.page.locator("#indet").isVisible()) && /chargeur/.test(await P.text("#mode")), await P.text("#indet"));
    check(S + " md5 mismatch", "no 'written' line", !/md5 verified by the loader/.test(capture().slice(c0)));
    await shot(P, "V-md5-mismatch"); await P.ctx.close();
    // 3. transfer complete but the MD5 request is never answered → indeterminate / written-unverified
    P = await fresh(); c0 = cap0();
    await P.sim(() => { __sim.cfg.md5Drop = true; });
    r = await wr([["p91/boot.bin", 0]]); st = await fin(r.req, 90000); res = await result(r.req);
    check(S + " md5 silent", "transfer done but MD5 unanswered → indeterminate / written-unverified", st.state === "indeterminate" && res.flash === "written-unverified" && res.ok === false, st.state + " " + res.flash);
    check(S + " md5 silent", "the write really reached the simulated flash, yet it is not announced as success", (await P.sim(() => __sim.flash.slice(0, 6000).some((b) => b !== 0xaa))) && !/md5 verified by the loader/.test(capture().slice(c0)));
    await shot(P, "V-md5-silent"); await P.ctx.close();
  },

  async background() {
    const S = "B-background-tab";
    const P = await fresh();
    // Headless Firefox keeps every tab "visible" (checked: a second tab brought to front changes nothing). The Page
    // Visibility API is therefore forced and the timers are held to a 1 s grid, which is what Firefox does to hidden tabs.
    const blank = await P.ctx.newPage(); await blank.goto("about:blank"); await blank.bringToFront(); await sleep(300);
    info(S, "real visibility with a second tab in front: " + (await P.page.evaluate(() => document.visibilityState)) + " (no real change possible headless)");
    await P.page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
      Object.defineProperty(document, "hidden", { value: true, configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
      __sim.throttle = true; __sim.throttleStep = 1000;
    });
    await sleep(500);
    check(S, "page notices the change and logs it in the capture (emulated visibility)", /tab hidden/.test(capture()));
    let t0 = Date.now();
    const idr = await enq("identify"); const sti = await fin(idr.req, 30000);
    check(S, "a read-only command is picked up and completed while the tab is hidden", sti.state === "completed", `${sti.state} in ${Date.now() - t0} ms`);
    t0 = Date.now();
    const r = await wr([["p91/boot.bin", 0]]); const st = await fin(r.req, 60000); const res = await result(r.req);
    check(S, "write while the tab is hidden → completed with md5 verified", st.state === "completed" && res.verification && res.verification.md5 === "verified", `${st.state} in ${Date.now() - t0} ms`);
    check(S, "flash = file", sha256(Buffer.from(await P.sim(() => Array.from(__sim.flash.slice(0, 6000))))) === sha256(fs.readFileSync(DIR + "/web/p91/boot.bin")));
    const n0 = P.requests.filter((x) => /\/api\/images/.test(x.url)).length; await sleep(8000);
    const n1 = P.requests.filter((x) => /\/api\/images/.test(x.url)).length;
    info(S, `inventory requests in 8 s while hidden: ${n1 - n0}`);
    check(S, "hidden tab does not accumulate inventory requests (≤ 3 in 8 s)", n1 - n0 <= 3);
    await P.sim(() => { __sim.cfg.beginDelayMs = 6500; });
    const r2 = await wr([["p91/boot.bin", 0]]); const st2 = await fin(r2.req, 90000);
    check(S, "late begin ack while hidden → indeterminate, and the trace dump records vis=hidden", st2.state === "indeterminate" && /vis=hidden/.test(capture()), st2.state);
    await P.page.evaluate(() => { delete document.visibilityState; delete document.hidden; __sim.throttle = false; document.dispatchEvent(new Event("visibilitychange")); });
    await blank.close(); await sleep(500);
    check(S, "back to visible: the page logs it", /tab visible/.test(capture()));
    await P.ctx.close();
  },

  async inventory() {
    const S = "I-inventory-600";
    const P = await fresh();
    await sleep(4000);
    const rows = await P.page.locator("#imgs tbody tr").count();
    info(S, "rows rendered: " + rows + " ; " + (await P.text("#invstate")));
    check(S, "inventory of 600+ files rendered", rows >= 600, rows);
    // forced cold rebuild while measuring the capture path
    for (const f of fs.readdirSync(DIR + "/web/p91/bulk")) { const p = DIR + "/web/p91/bulk/" + f; const t = new Date(); fs.utimesSync(p, t, t); }
    const lat = [];
    const inv = []; for (let i = 0; i < 4; i++) inv.push((async () => { const t = Date.now(); await fetch(Q + "/api/images?fresh=1"); return Date.now() - t; })());
    for (let i = 0; i < 20; i++) { const t = Date.now(); await fetch(Q + "/api/log", { method: "POST", body: JSON.stringify({ lines: [] }) }); lat.push(Date.now() - t); await sleep(20); }
    const invMs = await Promise.all(inv);
    info(S, `rebuild ms ${invMs.join("/")}; /api/log max latency during rebuild ${Math.max(...lat)} ms`);
    check(S, "capture path stays responsive during a forced inventory rebuild (max < 250 ms)", Math.max(...lat) < 250, Math.max(...lat));
    const t0 = Date.now(); const idr = await enq("identify"); const st = await fin(idr.req, 30000);
    check(S, "a command still completes promptly with 600+ files listed", st.state === "completed" && Date.now() - t0 < 8000, `${st.state} ${Date.now() - t0} ms`);
    // write: image lookup uses scope=all&archives=1&fresh=1 over the whole inventory
    const t1 = Date.now(); const r = await wr([["p91/boot.bin", 0]]); const sw = await fin(r.req, 40000);
    check(S, "write command resolved against the full inventory and completed", sw.state === "completed", `${sw.state} ${Date.now() - t1} ms`);
    // scope toggles
    await P.page.check("#showarch"); await P.page.check("#showall"); await sleep(1500);
    const line = await P.text("#invstate");
    check(S, "checking 'archives' and 'autres dossiers' widens the list (hidden counters drop to 0)", /masqués : 0 archives, 0 hors dossiers/.test(line), line);
    await shot(P, "I-inventory"); await P.ctx.close();
  },

  // stock esptool-js Transport vs BenchTransport, same Firefox, same simulated chip, throttled timers: is the false timeout reproducible?
  async ab() {
    const S = "AB-stock-vs-bench";
    await startServer();
    const html = `<!doctype html><meta charset=utf-8><title>ab</title><pre id=o></pre><script type=module>
import { ESPLoader, Transport, ESPError } from "https://unpkg.com/esptool-js@0.7.0/bundle.js";
import { makeBenchTransport } from "/bench-transport.js";
const { BenchTransport } = makeBenchTransport(Transport, ESPError);
const term = { clean(){}, writeLine(){}, write(){} };
window.runAB = async (which, delay, grid) => {
  __sim.cfg.beginDelayMs = 0; __sim.throttle = false;
  const port = await navigator.serial.requestPort();
  const tr = which === "stock" ? new Transport(port, false) : new BenchTransport(port, false);
  const l = new ESPLoader({ transport: tr, baudrate: 115200, terminal: term });
  await l.main();
  __sim.cfg.beginDelayMs = delay; __sim.throttle = true; __sim.throttleStep = grid;
  const t0 = performance.now(); let out;
  try { await l.flashDeflBegin(6000, 6011, 0); out = { ok: true }; } catch (e) { out = { ok: false, err: String(e.message || e).slice(0, 120), kind: e.kind }; }
  out.ms = Math.round(performance.now() - t0);
  __sim.throttle = false;
  try { await tr.disconnect(); } catch (e) {}
  return out;
};
window.abReady = true;
</script>`;
    const rows = [];
    for (const [which, delay, grid] of [["stock", 2900, 4000], ["bench", 2900, 4000], ["stock", 500, 4000], ["bench", 500, 4000], ["stock", 2500, 1000], ["bench", 2500, 1000]]) {
      const P = await newPage(browser, "AB");
      await P.page.route(Q + "/ab.html", (rt) => rt.fulfill({ contentType: "text/html", body: html }));
      await P.page.goto(Q + "/ab.html"); await P.page.waitForFunction(() => window.abReady, null, { timeout: 20000 });
      const o = await P.page.evaluate(([w, d, g]) => window.runAB(w, d, g), [which, delay, grid]);
      rows.push({ which, ack_ms: delay, timer_grid_ms: grid, ...o }); info(S, JSON.stringify(rows[rows.length - 1]));
      await P.ctx.close();
    }
    fs.writeFileSync(DIR + "/ab.json", JSON.stringify(rows, null, 1));
    const g = (w, d, gr) => rows.find((r) => r.which === w && r.ack_ms === d && r.timer_grid_ms === gr);
    check(S, "BenchTransport accepts an acknowledgement that arrives in time when timers wake on a 4 s grid", g("bench", 2900, 4000).ok === true, JSON.stringify(g("bench", 2900, 4000)));
    info(S, "stock Transport, same conditions: " + JSON.stringify(g("stock", 2900, 4000)));
    check(S, "stock Transport reproduces the exact M4-bis message when timer wake-ups are further apart than its limit, even for a 0.5 s answer", g("stock", 500, 4000).ok === false && /Possible serial noise or corruption/.test(g("stock", 500, 4000).err), JSON.stringify(g("stock", 500, 4000)));
    check(S, "with timers on a 1 s grid (ordinary hidden-tab clamp) the stock Transport still copes with a 2.5 s answer: the defect needs wake-ups later than its 3 s limit", g("stock", 2500, 1000).ok === true);
  },
};
const names = only && only !== "all" ? only.split(",") : Object.keys(scenarios);
for (const n of names) { try { await scenarios[n](); } catch (e) { check(n, "scenario ran to the end", false, e.stack.split("\n").slice(0, 3).join(" | ")); } }
await browser.close(); saveResults();
const bad = results.filter((x) => !x.pass).length; console.log(`\n${results.length - bad}/${results.length} PASS`);

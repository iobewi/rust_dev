import fs from "node:fs";
import { Q, DIR, check, info, j, enq, waitState, waitFor, sleep, startServer, launch, newPage, boot, sha256, shot, capture, events, results, saveResults, result, shaOf, status } from "./lib.mjs";
const only = process.argv[2];
const browser = await launch();
const wr = async (paths, extra = {}) => { const items = []; for (const [p, addr] of paths) items.push({ path: p, addr, sha: await shaOf(p) }); return enq("write", { items, ...extra }); };
const fin = (req, ms = 40000) => waitState(req, ["completed", "failed", "indeterminate", "expired"], ms);
const scenarios = {
  async server_restart() {
    const S = "S1-server-restart-mid-write";
    await startServer(); const P = await newPage(browser, "A"); await boot(P);
    await P.sim(() => { __sim.cfg.blockDelayMs = 400; __sim.cfg.closeAtBlock = 4; });   // the write ends indeterminate (port lost) after ~1.6 s
    const r = await wr([["p91/big.bin", 0x10000]]);
    await waitFor(async () => (await P.sim(() => __sim.cmds.filter((c) => c.op === 0x11).length)) >= 1, 15000);
    await startServer();                                    // the server restarts: its in-memory command table is gone
    const unknown = await status(r.req);
    info(S, "new server knows the req? " + JSON.stringify(unknown));
    const st = await waitFor(async () => { const s = await status(r.req); return s.state ? s : null; }, 30000);
    await sleep(500);
    const res = await result(r.req);
    info(S, "server record after the page's result: " + JSON.stringify(st) + " / result " + JSON.stringify(res).slice(0, 200));
    check(S, "page-side verdict is indeterminate (pill)", await P.page.locator("#indet").isVisible());
    check(S, "the server keeps the page's verdict 'indeterminate' for a result it did not queue (never downgrades it to failed/completed)", st && st.state === "indeterminate", st && st.state);
    await P.ctx.close();
  },
  async claim_without_ack() {
    const S = "S2-claim-without-ack";
    await startServer({ ACK: "2" });
    const q = await enq("mark", { text: "claimed-never-acked" });
    const got = await fetch(Q + "/api/cmd?wait=1&page=ghost").then((r) => r.json());   // a client takes it and never acknowledges
    check(S, "the ghost client was handed the command", got.req === q.req, JSON.stringify(got).slice(0, 80));
    await sleep(3000);
    const s = await status(q.req);
    check(S, "claimed but never acknowledged → Expired, executed=false", s.state === "expired" && s.executed === false, JSON.stringify(s));
    const late = await fetch(Q + "/api/ack", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ req: q.req, claim: got.claim, page: "ghost" }) });
    check(S, "a late acknowledgement is refused (409): the command must not start", late.status === 409, late.status);
    const P = await newPage(browser, "A"); await boot(P); await sleep(2500);
    check(S, "a live page that connects afterwards does not run it", !/claimed-never-acked/.test(capture()));
    await P.ctx.close();
  },
  async reopen_confirmation() {
    const S = "S3-open-console-confirmation";
    await startServer(); const P = await newPage(browser, "A"); await boot(P);
    await P.sim(() => { __sim.cfg.beginDelayMs = 3600; });
    const r = await wr([["p91/boot.bin", 0]]); await fin(r.req, 40000);
    await P.page.click("#disconnect"); await sleep(500);                       // operator releases the port; state is kept
    check(S, "indeterminate state kept after 'Libérer le port'", await P.page.locator("#indet").isVisible());
    const o0 = await P.sim(() => __sim.opens);
    P.dialogMode = "dismiss"; await P.page.click("#connect"); await sleep(800);
    check(S, "'Ouvrir la console' asks for confirmation; dismissed → port stays closed", (await P.sim(() => __sim.opens)) === o0 && P.dialogs.length >= 1, JSON.stringify(P.dialogs.slice(-1)));
    P.dialogMode = "accept"; await P.page.click("#connect"); await sleep(1000);
    check(S, "accepted by the operator → console opens (operator-controlled), state still shown until lifted", (await P.sim(() => __sim.opens)) > o0 && (await P.page.locator("#indet").isVisible()));
    P.dialogMode = "accept"; await P.page.click("#indetclear"); await sleep(300);
    check(S, "only the explicit 'Lever l'état indéterminé' + confirmation clears it", await P.page.locator("#indet").isHidden());
    await P.page.reload({ waitUntil: "load" }); await sleep(500);
    check(S, "and it stays cleared after a reload", await P.page.locator("#indet").isHidden());
    await P.ctx.close();
  },
};
const names = only && only !== "all" ? only.split(",") : Object.keys(scenarios);
for (const n of names) { try { await scenarios[n](); } catch (e) { check(n, "scenario ran to the end", false, e.stack.split("\n").slice(0, 3).join(" | ")); } }
await browser.close(); saveResults();
const bad = results.filter((x) => !x.pass).length; console.log(`\n${results.length - bad}/${results.length} PASS`);

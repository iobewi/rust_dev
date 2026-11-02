// A response cut at 64 KiB by an intermediary (relay, proxy) between the browser and the bench server.
// Incident P5-A: the write resolved its deliverable against the whole inventory (73 KB), received 65 536 bytes
// and failed in JSON.parse. Here the intermediary is a real HTTP proxy in front of the real server, no board, no port.
import http from "node:http";
import fs from "node:fs";
import { Q, DIR, check, info, enq, waitState, startServer, launch, newPage, boot, saveResults, results, shaOf, capture, sleep, result } from "./lib.mjs";
const PROXY = Number(new URL(Q).port) + 1;
const B = `http://127.0.0.1:${PROXY}`;
const cut = { n: 65536, hits: [] };                       // answers to /api/images* are cut to n bytes, silently (clean end of body)
const proxy = http.createServer((req, res) => {
  const up = http.request({ host: "127.0.0.1", port: Number(new URL(Q).port), method: req.method, path: req.url, headers: req.headers }, (ur) => {
    if (!req.url.startsWith("/api/images")) { res.writeHead(ur.statusCode, ur.headers); ur.pipe(res); return; }
    const chunks = []; ur.on("data", (c) => chunks.push(c)); ur.on("end", () => {
      const body = Buffer.concat(chunks); cut.hits.push({ url: req.url, full: body.length, sent: Math.min(body.length, cut.n) });
      const h = { ...ur.headers }; delete h["content-length"];
      res.writeHead(ur.statusCode, h); res.end(body.subarray(0, cut.n));
    });
  });
  req.pipe(up); up.on("error", () => res.destroy());
});
await new Promise((r) => proxy.listen(PROXY, "127.0.0.1", r));
const browser = await launch();
const fin = (req, ms = 40000) => waitState(req, ["completed", "failed", "indeterminate", "expired"], ms);
const wr = async (p) => enq("write", { items: [{ path: p, addr: 0, sha: await shaOf(p) }] });
async function fresh() { await startServer(); const P = await newPage(browser, "A"); await boot(P, { base: B }); await P.sim(() => { __sim.preload(0, new Uint8Array(6000).fill(0xaa)); }); await sleep(3000); return P; }
const flashUntouched = (P) => P.sim(() => __sim.flash.slice(0, 6000).every((b) => b === 0xaa));
const S = "T-truncated-inventory";

let P = await fresh();
cut.n = 65536; cut.hits.length = 0;
const full = (await fetch(Q + "/api/images?scope=all&archives=1&fresh=1")).headers.get("content-length");
info(S, `whole inventory: ${full} bytes; the intermediary lets ${cut.n} through`);
check(S, "precondition: the whole inventory is larger than 64 KiB", Number(full) > 65536, full);

// 1. the write does not depend on the size of the inventory any more
let t0 = Date.now(); let r = await wr("p91/boot.bin"); let st = await fin(r.req);
check(S, "write through an intermediary that cuts every /api/images answer at 65 536 bytes: completed", st.state === "completed", `${st.state} ${JSON.stringify(await result(r.req)).slice(0, 300)}`);
check(S, "the write's lookup was the named file only, a few hundred bytes", cut.hits.some((h) => /path=p91(%2F|\/)boot\.bin/.test(h.url) && h.full < 2000), JSON.stringify(cut.hits.map((h) => [h.url.slice(0, 80), h.full, h.sent])));
await P.ctx.close();

// 2. when the answer to the lookup itself is cut, the write is refused, says why, and nothing is programmed
P = await fresh(); cut.n = 100; cut.hits.length = 0;
const c0 = capture().length;
r = await wr("p91/boot.bin"); st = await fin(r.req); const res = await result(r.req);
check(S, "lookup cut to 100 bytes: failed (never completed, never indeterminate)", st.state === "failed", st.state);
const err = String(res.error || "");
info(S, "error: " + err);
check(S, "the error names the incomplete answer with its figures (HTTP status, content-type, content-length, bytes received)", /incomplète|illisible/.test(err) && /HTTP 200/.test(err) && /content-length=absent/.test(err) && /reçu=100/.test(err), err);
check(S, "nothing was programmed: simulated flash untouched", await flashUntouched(P));
check(S, "no flash operation was announced in the capture", !/write p91\/boot\.bin/.test(capture().slice(c0)));
check(S, "no indeterminate state recorded", await P.page.locator("#indet").isHidden());
// 3. the page recovers by itself once the intermediary lets the answer through
cut.n = 1 << 30; r = await wr("p91/boot.bin"); st = await fin(r.req);
check(S, "same command accepted again as a new request once the path is clear", st.state === "completed", st.state);
await P.ctx.close();

await browser.close(); proxy.close(); saveResults();
const bad = results.filter((x) => !x.pass).length; console.log(`\n${results.length - bad}/${results.length} PASS`);

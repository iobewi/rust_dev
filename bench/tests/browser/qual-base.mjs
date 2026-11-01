// Mission 2: the real page in Firefox with the real esptool-js 0.7.0 over the simulated port. Read-only operations.
import fs from "node:fs";
import crypto from "node:crypto";
import { Q, DIR, check, info, j, enq, waitState, waitFor, sleep, startServer, launch, newPage, boot, sha256, shot, capture, results, saveResults, result } from "./lib.mjs";

const S = "base";
await startServer();
const browser = await launch();
info(S, "firefox " + browser.version());
const P = await newPage(browser, "A");
await boot(P, { full: false });

// ---- modules and library actually loaded
const reqs = P.requests.map((r) => r.url.replace(Q, ""));
for (const m of ["/", "/bench-transport.js", "/flash-op.js", "/cmd-client.js", "/poller.js", "/md5.js"]) {
  if (m === "/md5.js") continue;   // imported by flash-op.js, and by the simulator
}
check(S, "page loaded without script error", P.errors.length === 0, P.errors.join(" | "));
for (const m of ["/bench-transport.js", "/flash-op.js", "/cmd-client.js", "/poller.js"]) check(S, "module requested: " + m, reqs.includes(m));
const eu = P.responses.find((r) => /esptool-js@0\.7\.0\/bundle\.js/.test(r.url));
check(S, "esptool-js@0.7.0 bundle.js fetched from unpkg (HTTP 200)", eu && eu.status === 200, eu && eu.url);
const mine = Buffer.from(await (await fetch("https://unpkg.com/esptool-js@0.7.0/bundle.js")).arrayBuffer());
check(S, "bundle served to the page = the pinned 0.7.0 bundle fetched independently (same sha256)", eu && sha256(eu.bytes) === sha256(mine), eu && sha256(eu.bytes).slice(0, 16));
info(S, "bundle bytes " + (eu && eu.bytes.length));
const lt = await P.page.evaluate(() => typeof navigator.serial + " " + typeof window.__sim);
info(S, "navigator.serial/sim: " + lt);

// ---- console and states
const log0 = await P.log();
check(S, "console shows the boot lines of the simulated board", /iobewi ready/.test(log0));
check(S, "mode pill = console", (await P.text("#mode")) === "console");
check(S, "indeterminate pill hidden at start", await P.page.locator("#indet").isHidden());
await shot(P, "base-console");

// ---- identify through the real library (sync, GET_SECURITY_INFO, read_reg, change baud, flash id)
let o0 = await P.sim(() => __sim.opens);
let r = await enq("identify");
let st = await waitState(r.req, ["completed", "failed", "indeterminate", "expired"], 30000);
let res = await result(r.req);
check(S, "identify via /api/cmd → completed", st.state === "completed", JSON.stringify(st));
info(S, "identify result: " + JSON.stringify(res.value || res).slice(0, 260));
check(S, "chip reported = ESP32-S3 (from the library, not the page)", /ESP32-S3/.test(JSON.stringify(res)));
const lg = await P.log();
check(S, "esptool-js lines in the capture (Subclass of Transport usable by ESPLoader)", /loader connected: ESP32-S3/.test(lg), (lg.match(/loader connected[^\n]*/) || [""])[0]);
check(S, "console reopened after the operation (default behaviour on success)", (await P.text("#mode")) === "console" && (await P.sim(() => __sim.opens)) > o0);

// ---- partition table + a read-back through the stream of READ_FLASH frames
const pt = new Uint8Array(0xc00).fill(0xff);
const ent = (i, type, sub, off, size, label) => { const b = new DataView(pt.buffer, i * 32, 32); pt[i * 32] = 0xaa; pt[i * 32 + 1] = 0x50; pt[i * 32 + 2] = type; pt[i * 32 + 3] = sub; b.setUint32(4, off, true); b.setUint32(8, size, true); for (let k = 0; k < label.length; k++) pt[i * 32 + 12 + k] = label.charCodeAt(k); };
ent(0, 1, 2, 0x9000, 0x6000, "nvs"); ent(1, 0, 0x10, 0x10000, 0x100000, "ota_0");
await P.sim((a) => __sim.preload(0x8000, Uint8Array.from(a)), [...pt]);
r = await enq("readpt", { addr: 0x8000 });
st = await waitState(r.req, ["completed", "failed", "indeterminate"], 30000);
check(S, "readpt → completed", st.state === "completed", JSON.stringify(st));
await sleep(300);
check(S, "partition table rendered in the page (2 rows + table)", (await P.page.locator("#pt tbody tr").count()) === 3, await P.page.locator("#pt tbody tr").count());

r = await enq("dump", { regions: [{ name: "q-base", addr: 0x8000, size: 0xc00 }] });
st = await waitState(r.req, ["completed", "failed", "indeterminate"], 30000);
check(S, "dump → completed", st.state === "completed", JSON.stringify(st));
const saved = fs.existsSync(DIR + "/logs/dumps/q-base.bin") ? fs.readFileSync(DIR + "/logs/dumps/q-base.bin") : null;
check(S, "read-back saved by the server = flash content of the simulated chip (sha256)", saved && sha256(saved) === (await P.sim(() => __sim.sha256(0x8000, 0xc00))), saved && sha256(saved).slice(0, 16));

// ---- API surface used by the page
const calls = (re) => P.requests.filter((q) => re.test(q.url)).length;
info(S, `requests: /api/images ${calls(/\/api\/images/)}, /api/cmd ${calls(/\/api\/cmd/)}, /api/ack ${calls(/\/api\/ack/)}, /api/result ${calls(/\/api\/result/)}, /api/log ${calls(/\/api\/log/)}, /api/info ${calls(/\/api\/info/)}, /api/dump ${calls(/\/api\/dump/)}`);
check(S, "page calls /api/images, /api/cmd, /api/ack, /api/result", calls(/\/api\/images/) > 0 && calls(/\/api\/cmd/) > 0 && calls(/\/api\/ack/) >= 3 && calls(/\/api\/result/) >= 3);
check(S, "page does NOT call /api/info (only the operator/tools do)", calls(/\/api\/info/) === 0);
const inf = await j("/api/info");
check(S, "/api/info (direct) points to the temporary directories", inf.capture.startsWith(DIR + "/logs") && inf.web === DIR + "/web", inf.capture);

// ---- inventory shown by the page
await sleep(3500);
const inv = (await P.text("#invstate"));
info(S, "inventory line: " + inv);
check(S, "inventory summary shows files / unique digests / ignored JSON / hidden", /fichiers/.test(inv) && /empreintes uniques/.test(inv) && /JSON ignorés/.test(inv) && /masqués/.test(inv));
const cats = await P.page.locator("#imgs tbody tr td:nth-child(3)").allInnerTexts();
check(S, "categories visible in the table", new Set(cats).size >= 2, [...new Set(cats)].join(","));
await shot(P, "base-inventory");

// ---- no accumulation of /api/images requests: sample 14 s, at most one in flight, spacing >= ~3 s
const t0 = Date.now();
await sleep(14000);
const im = P.requests.filter((q) => /\/api\/images/.test(q.url) && q.t >= t0).sort((a, b) => a.t - b.t);
let overlap = 0, minGap = 1e9;
for (let i = 1; i < im.length; i++) { if (im[i].t < (im[i - 1].end || im[i - 1].t)) overlap++; minGap = Math.min(minGap, im[i].t - (im[i - 1].end || im[i - 1].t)); }
info(S, `inventory requests in 14 s: ${im.length}, overlapping ${overlap}, min gap after previous ended ${minGap} ms`);
check(S, "inventory refresh: 3–5 requests in 14 s, none overlapping, gap ≥ 2.9 s", im.length >= 3 && im.length <= 5 && overlap === 0 && minGap >= 2900);
check(S, "no JS error during the whole run", P.errors.length === 0, P.errors.join(" | "));
const warn = P.consoleMsgs.filter((m) => /^(error|warning)/.test(m));
info(S, "browser console warnings/errors: " + (warn.length ? warn.slice(0, 5).join(" || ") : "none"));

await browser.close();
saveResults();
const bad = results.filter((x) => !x.pass).length;
console.log(`\n${S}: ${results.length - bad}/${results.length} PASS`);

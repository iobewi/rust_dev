// Shared helpers for the Firefox qualification run on :8082 (test instance only).
import { firefox } from "playwright-core";
import { spawn, execSync } from "node:child_process";
import fs from "node:fs";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

// HERE: this directory (simulator, fixture). WORK: scratch directory (logs, test data, screenshots, downloaded browsers);
// nothing in it is versioned. The test instance never uses :8080 / :8081 (the live benches).
export const HERE = path.dirname(fileURLToPath(import.meta.url));
export const DIR = process.env.BENCH_QUAL_WORK || path.join(os.tmpdir(), "bench-qual");
const PORT = Number(process.env.BENCH_QUAL_PORT || 8082);
if ([8080, 8081].includes(PORT)) throw new Error("refusing to use a live bench port");
process.env.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(DIR, "browsers");
fs.mkdirSync(DIR, { recursive: true });
fs.rmSync(DIR + "/logs", { recursive: true, force: true });
execSync(`python3 "${HERE}/fixture.py" "${DIR}/web"`, { stdio: "ignore" });

export const Q = `http://127.0.0.1:${PORT}`;
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const results = [];
export function check(scn, name, cond, detail = "") {
  results.push({ scn, name, pass: !!cond, detail: String(detail).slice(0, 400) });
  console.log(`${cond ? "PASS" : "FAIL"} [${scn}] ${name}${detail ? "  — " + String(detail).slice(0, 300) : ""}`);
  return !!cond;
}
export function info(scn, msg) { console.log(`     [${scn}] ${msg}`); }
export const j = async (path, body) => {
  const r = await fetch(Q + path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const t = await r.text(); let o = {}; try { o = JSON.parse(t); } catch (e) { o = { raw: t.slice(0, 80) }; }
  return { code: r.status, ...o };
};
export const status = (req) => j("/api/status?req=" + req);
export const result = (req) => j("/api/result?req=" + req);
export async function waitState(req, states, ms = 60000) {
  const t0 = Date.now();
  for (;;) {
    const s = await status(req);
    if (states.includes(s.state)) return s;
    if (Date.now() - t0 > ms) return { ...s, timeout: true };
    await sleep(150);
  }
}
export async function waitFor(fn, ms = 20000, step = 100) {
  const t0 = Date.now();
  for (;;) { const v = await fn(); if (v) return v; if (Date.now() - t0 > ms) return null; await sleep(step); }
}
let n = 0;
export const enq = (op, extra = {}) => { const req = `q${Date.now().toString(36)}${(n++).toString(36)}`; return j("/api/cmd", { op, req, ttl: 60, ...extra }).then((r) => ({ ...r, req })); };

// ---- server lifecycle (the test instance only)
export function serverPid() { try { return +fs.readFileSync(DIR + "/server.pid", "utf8"); } catch (e) { return 0; } }
export async function stopServer() {
  const pid = serverPid();
  if (pid) { try { process.kill(pid, "SIGTERM"); } catch (e) {} }
  await waitFor(async () => { try { await fetch(Q + "/api/info"); return false; } catch (e) { return true; } }, 8000);
}
export async function startServer(env = {}) {
  await stopServer();
  const server = path.join(HERE, "../../server/bench_server.py");
  const child = spawn("python3", [server], { env: { ...process.env, BENCH_PORT: String(PORT), BENCH_LOGS: DIR + "/logs", BENCH_WEB: DIR + "/web",
    BENCH_ACK_TIMEOUT: env.ACK || "15", BENCH_CMD_TTL: env.TTL || "120" }, detached: true, stdio: ["ignore", fs.openSync(DIR + "/server.out", "a"), fs.openSync(DIR + "/server.out", "a")] });
  child.unref(); fs.writeFileSync(DIR + "/server.pid", String(child.pid));
  const ok = await waitFor(async () => { try { return (await fetch(Q + "/api/info")).ok; } catch (e) { return false; } }, 10000);
  if (!ok) throw new Error("test server did not start");
  return child.pid;
}
export const capture = () => { try { return fs.readFileSync(DIR + "/logs/serial.log", "utf8"); } catch (e) { return ""; } };
export const events = () => { try { return fs.readFileSync(DIR + "/logs/commands.jsonl", "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)); } catch (e) { return []; } };

// ---- browser
export async function launch() {
  return firefox.launch({ headless: true });
}
export async function newPage(browser, name = "A", opts = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
  const page = await ctx.newPage();
  const P = { name, ctx, page, requests: [], errors: [], consoleMsgs: [], dialogs: [], dialogMode: "accept", responses: [] };
  page.on("request", (r) => P.requests.push({ t: Date.now(), url: r.url(), method: r.method() }));
  page.on("requestfinished", (r) => { const x = P.requests.find((q) => q.url === r.url() && !q.end); if (x) x.end = Date.now(); });
  page.on("pageerror", (e) => P.errors.push(String(e)));
  page.on("console", (m) => P.consoleMsgs.push(`${m.type()}: ${m.text()}`.slice(0, 300)));
  page.on("dialog", async (d) => { P.dialogs.push({ type: d.type(), message: d.message().slice(0, 200), mode: P.dialogMode }); P.dialogMode === "accept" ? await d.accept() : await d.dismiss(); });
  page.on("response", async (r) => { if (r.url().includes("unpkg.com")) P.responses.push({ url: r.url(), status: r.status(), bytes: (await r.body().catch(() => Buffer.alloc(0))) }); });
  await page.addInitScript({ path: HERE + "/sim.js" });
  P.sim = (fn, arg) => page.evaluate(fn, arg);
  P.text = (sel) => page.locator(sel).first().innerText();
  P.log = () => page.locator("#log").innerText();
  return P;
}
// Load the real page, connect the (simulated) port, wait for the console, optionally grant full access.
export async function boot(P, { full = true, tab = true } = {}) {
  await P.page.goto(Q + "/", { waitUntil: "load" });
  await waitFor(async () => (await P.text("#mode")) !== undefined, 5000);
  const hasPort = (await P.text("#mode")) === "port libre";
  await P.page.click("#connect");
  const up = await waitFor(async () => (await P.text("#mode")) === "console", 15000);
  if (!up) throw new Error("console did not open: " + (await P.text("#mode")));
  if (full) { await P.page.click("#grant"); await waitFor(async () => /accès complet/.test(await P.text("#access")), 3000); }
  return up;
}
export const sha256 = (buf) => crypto.createHash("sha256").update(buf).digest("hex");
export async function images() { return (await j("/api/images?scope=all&archives=1&fresh=1")).images; }
export const shaOf = async (path) => (await images()).find((i) => i.path === path).sha256;
export const shot = async (P, name) => { fs.mkdirSync(DIR + "/shots", { recursive: true }); await P.page.screenshot({ path: `${DIR}/shots/${name}.png`, fullPage: false }); };
export const saveResults = () => {
  fs.writeFileSync(DIR + "/results-" + (process.argv[2] || "all") + ".json", JSON.stringify(results, null, 1));
  process.exitCode = results.some((r) => !r.pass) ? 1 : 0;
};
process.on("exit", () => { const pid = serverPid(); if (pid) { try { process.kill(pid, "SIGTERM"); } catch (e) {} } });   // only the pid this harness started

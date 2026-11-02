// The page's script run against stubs: catches load-time errors (renamed variables, bad imports, TDZ) that no unit test sees.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { makeBenchTransport } from "../web/bench-transport.js";
import { Tracer, runWrite } from "../web/flash-op.js";
import { makeClient, consoleGuard } from "../web/cmd-client.js";
import { makePoller } from "../web/poller.js";
import { fetchJson } from "../web/fetch-json.js";
import { ESPError, FakeTransport } from "./fakes.mjs";

const html = readFileSync(new URL("../web/index.html", import.meta.url), "utf8");
const body = html.match(/<script type="module">([\s\S]*)<\/script>/)[1].replace(/^import .*$/gm, "");

const el = () => {
  const t = { tBodies: [{ innerHTML: "" }], childNodes: [], style: {}, dataset: {}, classList: { add() {}, remove() {} }, value: "", checked: true, textContent: "",
    querySelectorAll: () => [], addEventListener() {}, appendChild() {}, removeChild() {}, scrollTop: 0, clientHeight: 0, scrollHeight: 0 };
  return new Proxy(t, { get: (o, k) => (k in o ? o[k] : undefined), set: (o, k, v) => ((o[k] = v), true) });
};

test("the page loads, renders the inventory summary and posts its first trace line", async () => {
  const els = new Map(); const posted = [];
  const document = { getElementById: (id) => { if (!els.has(id)) els.set(id, el()); return els.get(id); }, addEventListener() {}, visibilityState: "visible", createElement: () => el(), querySelectorAll: () => [] };
  const inventory = { images: [{ path: "p91/a.bin", size: 4, mtime: 1, sha256: "ab".repeat(32), source: "web", category: "artifact", origin: "p91", allowed: true }],
    manifests: [], ignored_json: [{ path: "c33/firmware-layout.json", reason: "not-esp-web-tools" }],
    counts: { files: 1, unique_sha256: 1, duplicate_groups: 0, by_category: { artifact: 1 } }, excluded: { archives: 2, outside_allowed: 3 }, scope: {} };
  const fetch = async (url, opt) => {
    if (String(url).startsWith("/api/log")) posted.push(JSON.parse(opt.body));
    const out = String(url).startsWith("/api/images") ? inventory : {};
    const bytes = new TextEncoder().encode(JSON.stringify(out));
    return { ok: true, status: 200, headers: { get: (k) => (k === "content-length" ? String(bytes.length) : null) }, json: async () => out, arrayBuffer: async () => bytes.buffer };
  };
  const unref = (f, ms) => { const t = setTimeout(f, ms); t.unref(); return t; };
  const g = { document, navigator: { serial: { getPorts: async () => [], addEventListener() {} } }, fetch, localStorage: { getItem: () => null, setItem() {} },
    crypto: { subtle: {}, randomUUID: () => "abcdef01-0000" }, confirm: () => false, performance, console, setTimeout: unref, clearTimeout, setInterval: () => 0, URLSearchParams, URL };
  const { BenchTransport } = makeBenchTransport(FakeTransport, ESPError);
  const names = ["ESPLoader", "Transport", "ESPError", "makeBenchTransport", "Tracer", "runWrite", "makeClient", "consoleGuard", "makePoller", "fetchJson", ...Object.keys(g)];
  // the page's own timers must not keep node alive
  const poller = (task, ms) => makePoller(task, ms, { setTimeout: unref, clearTimeout });
  const values = [class {}, FakeTransport, ESPError, makeBenchTransport, Tracer, runWrite, makeClient, consoleGuard, poller, (u, i) => fetchJson(u, i, fetch), ...Object.values(g)];
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  await new AsyncFunction(...names, body)(...values);
  await new Promise((r) => setTimeout(r, 80));
  assert.match(els.get("invstate").textContent, /1 fichiers, 1 empreintes uniques/);
  assert.match(els.get("invstate").textContent, /1 JSON ignorés/);
  assert.match(els.get("invstate").textContent, /2 archives, 3 hors dossiers déclarés/);
  assert.match(els.get("imgs").tBodies[0].innerHTML, /p91\/a\.bin/);
  const lines = posted.flatMap((p) => p.lines.map((l) => l.s)).join("\n");
  assert.match(lines, /page loaded: auto reset/);
  assert.match(lines, /page id abcdef01/);
});

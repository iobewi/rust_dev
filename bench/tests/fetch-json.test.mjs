import test from "node:test";
import assert from "node:assert/strict";
import { fetchJson } from "../web/fetch-json.js";

// A response as an intermediary can deliver it: `body` is what arrives, `declared` what the server announced.
const resp = (body, { status = 200, declared, type = "application/json" } = {}) => {
  const bytes = new TextEncoder().encode(body);
  const h = new Map([["content-type", type]]);
  if (declared !== undefined) h.set("content-length", String(declared));
  return { status, ok: status >= 200 && status < 300, headers: { get: (k) => h.get(k.toLowerCase()) ?? null }, arrayBuffer: async () => bytes.buffer.slice(0) };
};
// A JSON document whose serialization is exactly `n` bytes.
const doc = (n) => { const base = JSON.stringify({ images: [], pad: "" }).length; return JSON.stringify({ images: [], pad: "x".repeat(n - base) }); };

for (const n of [4096, 32768, 65535, 65536, 65537, 131072]) {
  test(`${n} bytes, complete: parsed`, async () => {
    const d = doc(n); assert.equal(d.length, n);
    const j = await fetchJson("/api/images?fresh=1", {}, async () => resp(d, { declared: n }));
    assert.deepEqual(j.images, []);
  });
  test(`${n} bytes, cut at 65536 with the length still announced: refused, with figures`, async () => {
    const d = doc(n);
    if (n <= 65536) return;
    await assert.rejects(fetchJson("/api/images", {}, async () => resp(d.slice(0, 65536), { declared: n })),
      (e) => /incomplète/.test(e.message) && e.message.includes("65536") && e.message.includes(String(n)));
  });
  test(`${n} bytes, cut at 65536 and the length no longer announced (intermediary): refused, never an empty answer`, async () => {
    const d = doc(n);
    if (n <= 65536) return;
    await assert.rejects(fetchJson("/api/images", {}, async () => resp(d.slice(0, 65536))),
      (e) => /illisible|incomplète/.test(e.message) && e.message.includes("65536") && e.message.includes("content-length=absent"));
  });
}

test("HTTP error is not an answer", async () => {
  await assert.rejects(fetchJson("/api/images", {}, async () => resp("not found", { status: 404, type: "text/plain" })), /HTTP 404/);
});
test("a diagnostic shows the first and last characters only when asked", async () => {
  const cut = doc(70000).slice(0, 65536);
  await assert.rejects(fetchJson("/api/images", { peek: true }, async () => resp(cut)), /début "\{"images"/);
  await assert.rejects(fetchJson("/api/images", {}, async () => resp(cut)), (e) => !/début/.test(e.message));
});

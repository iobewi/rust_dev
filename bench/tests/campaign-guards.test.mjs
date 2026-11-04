import test from 'node:test';
import assert from 'node:assert/strict';
import { EspEngine } from '../web/esp-engine.js';

for (const reason of ['expired', 'revoked', 'busy', 'quarantine', 'loader']) {
  test(`matched delayed reset does not run after ${reason}`, async () => {
    let live = true, resets = 0;
    const engine = new EspEngine({ getInfo: () => ({}) }, {}, () => {});
    engine.mode = 'console';
    engine.reset = async () => { resets++; };
    await engine.execute('trigger', { data: 'YQ==', action: 'reset', delay_ms: 25,
      expires_ms: reason === 'expired' ? 10 : 10000 }, { check() {}, live: () => live });
    engine.matchTrigger(Uint8Array.of(97));
    if (reason === 'revoked') live = false;
    if (reason === 'busy') engine.busy = true;
    if (reason === 'quarantine') engine.hold = true;
    if (reason === 'loader') engine.mode = 'loader';
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(resets, 0);
    engine.cancelTrigger();
  });
}

test('LAN request validates its origin before fetch and never claims an opaque response is verified', async () => {
  const original = globalThis.fetch, calls = [];
  globalThis.fetch = async (...args) => { calls.push(args); return new Response('unread body'); };
  const engine = new EspEngine({ getInfo: () => ({}) }, {}, () => {});
  const context = { check() {}, device: { http_origins: ['http://192.168.100.2'] } };
  try {
    for (const url of ['http://192.168.100.3/', 'https://192.168.100.2/', 'http://example.com/',
      'http://user:secret@192.168.100.2/', 'http://127.0.0.1/']) {
      await assert.rejects(engine.execute('http.request', { url }, context),
        error => error.code === 'hardware_command_failed' && error.detail.phase === 'control');
    }
    assert.equal(calls.length, 0);
    const result = await engine.execute('http.request', { url: 'http://192.168.100.2/status' }, context);
    assert.deepEqual(result, { response: 'opaque', http_status: 'unknown', verified: false });
    assert.equal(calls.length, 1);
    const options = calls[0][1];
    assert.equal(options.credentials, 'omit'); assert.equal(options.redirect, 'error');
    assert.equal(options.mode, 'no-cors'); assert.equal(options.cache, 'no-store');
  } finally { globalThis.fetch = original; }
});

test('LAN request failure or abort is never a successful opaque response and releases the engine', async () => {
  const original = globalThis.fetch;
  const engine = new EspEngine({ getInfo: () => ({}) }, {}, () => {});
  const context = { check() {}, device: { http_origins: ['http://192.168.100.2'] } };
  try {
    const failure = new TypeError('Network or redirect refused');
    globalThis.fetch = async () => { throw failure; };
    await assert.rejects(engine.execute('http.request', { url: 'http://192.168.100.2/' }, context),
      error => error.code === 'hardware_command_failed' && error.name === 'TypeError' && !error.message.includes('Network or redirect'));
    assert.equal(engine.busy, false);
    globalThis.fetch = async (_url, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), { once: true });
    });
    await assert.rejects(engine.execute('http.request', { url: 'http://192.168.100.2/', timeout_ms: 10 }, context),
      error => error.name === 'AbortError');
    assert.equal(engine.busy, false);
  } finally { globalThis.fetch = original; }
});

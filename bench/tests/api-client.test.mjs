import test from 'node:test';
import assert from 'node:assert/strict';
import { BenchClient } from '../web/api-client.js';
import { sha256 } from '../web/protocol.js';

test('SSE client reassembles fragmented event batches and releases the stream on cancellation', async () => {
  const original = globalThis.fetch, encoder = new TextEncoder(); let cancelled = false, request;
  const event = { epoch: 'e', next: 7, lost: 0, events: [{ kind: 'mark', detail: 'repère' }] };
  const bytes = encoder.encode('data: ' + JSON.stringify(event) + '\n\n');
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return new Response(new ReadableStream({ start(controller) {
      for (let i = 0; i < bytes.length; i += 3) controller.enqueue(bytes.slice(i, i + 3));
    }, cancel() { cancelled = true; } }));
  };
  try {
    const iterator = new BenchClient('secret').events('a/b', 3, new AbortController().signal);
    assert.deepEqual((await iterator.next()).value, event); await iterator.return();
    assert.equal(cancelled, true); assert.ok(request.url.includes('a%2Fb'));
    assert.equal(request.options.headers.Authorization, 'Bearer secret'); assert.equal(request.url.includes('secret'), false);
  } finally { globalThis.fetch = original; }
});
test('dump downloads verify size and digest before offering bytes to the UI', async () => {
  const original = globalThis.fetch, bytes = new Uint8Array([0, 255, 13, 10]);
  const artifact = { id: 'dump', size: bytes.length, sha256: await sha256(bytes) };
  globalThis.fetch = async () => new Response(bytes);
  try {
    const client = new BenchClient('secret'); assert.deepEqual(await client.download(artifact), bytes);
    await assert.rejects(client.download({ ...artifact, size: 99 }), /Intégrité/);
    await assert.rejects(client.download({ ...artifact, sha256: '0'.repeat(64) }), /Intégrité/);
  } finally { globalThis.fetch = original; }
});

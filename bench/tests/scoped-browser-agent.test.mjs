import test from 'node:test';
import assert from 'node:assert/strict';
import { Agent } from '../web/agent.js';

test('scoped browser agent authenticates with logical board ID, not a chosen agent ID', async () => {
  const originalLocation = globalThis.location;
  globalThis.location = { href: 'http://localhost:8080/', protocol: 'http:' };
  try {
    const frames = [];
    const socket = {
      readyState: 1,
      bufferedAmount: 0,
      send(data) { frames.push(JSON.parse(data)); },
      close() {},
    };
    const agent = new Agent({
      engine: { busy: false },
      device: { id: 'board-a' },
      scopedBrowserAgent: true,
      reconnect: false,
      socketFactory: () => socket,
    });
    agent.connect();
    await socket.onopen();
    assert.deepEqual(frames[0], { type: 'auth', version: 1, device: 'board-a' });
    agent.disconnect();
  } finally {
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
  }
});

test('remote token agent retains its unchanged authentication contract', async () => {
  const originalLocation = globalThis.location;
  globalThis.location = { href: 'http://localhost:8080/', protocol: 'http:' };
  try {
    const frames = [];
    const socket = { readyState: 1, bufferedAmount: 0, send(data) { frames.push(JSON.parse(data)); }, close() {} };
    const agent = new Agent({ engine: { busy: false }, device: { id: 'board-a' },
      token: 'agent-secret', scopedBrowserAgent: true, reconnect: false, socketFactory: () => socket });
    agent.connect();
    await socket.onopen();
    assert.deepEqual(frames[0], { type: 'auth', version: 1, token: 'agent-secret' });
    agent.disconnect();
  } finally {
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
  }
});

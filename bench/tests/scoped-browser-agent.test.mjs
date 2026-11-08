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

test('two browser agents renew one expired session before authenticating', async () => {
  const priorLocation = globalThis.location, priorFetch = globalThis.fetch;
  globalThis.location = { href: 'http://localhost:8080/', protocol: 'http:' };
  let release, requests = 0;
  globalThis.fetch = (url, options) => {
    assert.equal(url, '/session/v1/open');
    assert.equal(options.credentials, 'same-origin');
    requests++;
    return new Promise(resolve => { release = resolve; });
  };
  const frames = [], sockets = [], agents = [];
  try {
    for (const id of ['board-a', 'board-b']) {
      const socket = { readyState: 1, bufferedAmount: 0,
        send(data) { frames.push(JSON.parse(data)); }, close() {} };
      const agent = new Agent({ engine: { busy: false }, device: { id },
        scopedBrowserAgent: true, socketFactory: () => socket, reconnect: false });
      agent.reconnectAttempt = true;
      agent.connect();
      sockets.push(socket); agents.push(agent);
    }
    const opening = sockets.map(socket => socket.onopen());
    assert.equal(requests, 1);
    assert.deepEqual(frames, []);
    release({ ok: true });
    await Promise.all(opening);
    assert.deepEqual(frames.map(frame => frame.device).sort(), ['board-a', 'board-b']);
    assert.equal(requests, 1);
  } finally {
    for (const agent of agents) agent.disconnect();
    if (priorLocation === undefined) delete globalThis.location;
    else globalThis.location = priorLocation;
    if (priorFetch === undefined) delete globalThis.fetch;
    else globalThis.fetch = priorFetch;
  }
});

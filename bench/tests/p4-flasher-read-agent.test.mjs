// REAL Agent + EspEngine + runP4FlasherRead against the simulated stub: a recipe longer than the start TTL must not stop on it.
import test from 'node:test';
import assert from 'node:assert/strict';
import {Agent} from '../web/agent.js';
import {EspEngine} from '../web/esp-engine.js';
import {bytes, sha, device, argsFor, fake} from './p4-flasher-fake.mjs';
import {P4_FLASHER_READ} from '../web/p4-flasher-read.js';

async function run({startMs, executionMs, delayMs}) {
  const loader = fake();
  const read = loader.transport.read;
  loader.transport.read = async timeout => { if (loader.IS_STUB) await new Promise(r => setTimeout(r, delayMs)); return read(timeout); };   // a slow board
  const engine = new EspEngine({getInfo: () => ({})}, {}, () => {});
  engine.romOnly = true; engine.native = true; engine.usbSerialJtag = false; engine.chipDescription = 'ESP32-P4';
  engine.openLoader = async () => loader;
  const agent = new Agent({engine, device, token: 'secret'});
  const sent = [];
  agent.socket = {readyState: 1, bufferedAmount: 0, send: s => sent.push(JSON.parse(s))};
  agent.generation = 'gen'; agent.serverOffset = 1000; agent.grantUntil = performance.now() + 60000;
  agent.artifact = async () => bytes;
  const plan = [{op: 'read', address: 0, size: 12288, packet_size: 4096}, {op: 'md5', address: 0, size: 4096}];
  const id = 'op1', now = performance.now();
  await agent.message({type: 'offer', id, generation: 'gen', command: P4_FLASHER_READ, args: await argsFor(plan), lease: 'lease',
    deadline_ms: now + 1000 + startMs, remaining_ms: startMs, lease_deadline_ms: now + 1000 + 60000});
  await agent.message({type: 'execute', id, execution_deadline_ms: now + 1000 + executionMs, deadline_ms: now + 1000 + startMs, remaining_ms: startMs});
  return sent.find(m => m.type === 'result');
}

test('the start TTL expires while the read is running: the operation still completes', async () => {
  const message = await run({startMs: 150, executionMs: 30000, delayMs: 60});   // ~10 stub frames x 60 ms >> 150 ms
  assert.equal(message.state, 'completed', JSON.stringify(message.error));
  assert.equal(message.result.steps[0].md5_match, true);
});

test('the execution deadline still stops it', async () => {
  const message = await run({startMs: 150, executionMs: 400, delayMs: 60});
  assert.notEqual(message.state, 'completed');
  assert.match(JSON.stringify(message), /execution_deadline_exceeded/);
});

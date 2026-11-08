// Clock, latency and suspension behaviour of the browser agent's deadlines (issue #2, M2 diagnostic).
// Server deadlines are on the server's monotonic clock; the agent maps them with ONE offset measured at the welcome
// (serverOffset = server_ms - authSent) and compares them with its own performance.now(). Nothing here changes agent.js:
// these tests document that every error of the mapping is either exact or fails CLOSED (never extends a deadline).
import test from 'node:test';
import assert from 'node:assert/strict';
import { Agent } from '../web/agent.js';

function setup(clock, execute) {
  const engine = { hold: false, persistHold() {}, setHold(value) { this.hold = value; }, execute };
  const agent = new Agent({ engine, device: { id: 'board' }, token: 'secret', now: () => clock.t });
  const messages = [];
  agent.socket = { readyState: 1, bufferedAmount: 0, send: s => messages.push(JSON.parse(s)) };
  agent.generation = 'gen'; agent.grantUntil = 1e12;
  return { agent, engine, messages };
}
const offer = (serverNow, ttl = 30000) => ({ type: 'offer', id: 'op', generation: 'gen', command: 'identify', args: {}, lease: 'lease',
  deadline_ms: serverNow + ttl, remaining_ms: ttl, lease_deadline_ms: serverNow + 600000, timeout_ms: 120000 });
const execute = (serverNow, startRemaining, run = 120000) => ({ type: 'execute', id: 'op', remaining_ms: startRemaining,
  deadline_ms: serverNow + startRemaining, execution_deadline_ms: serverNow + run });

test('welcome offset is conservative: handshake latency can only shorten a deadline', async () => {
  const clock = { t: 1000 };
  const x = setup(clock, async () => ({}));
  x.agent.authSent = clock.t;                                   // auth sent at browser 1000, server stamped 5000 after 40 ms of latency
  await x.agent.message({ type: 'welcome', server_ms: 5040 });
  assert.equal(x.agent.serverOffset, 4040);                     // true offset is 4000: the extra 40 ms moves every deadline EARLIER
  assert.equal(5000 + 30000 - x.agent.serverOffset - 100, 1000 + 30000 - 140);
});

test('a start window is exact on a stable clock', async () => {
  const clock = { t: 100000 };
  const seen = [];
  const x = setup(clock, async (command, args, ctx) => { seen.push(ctx.check); return { ok: true }; });
  x.agent.serverOffset = 4000;                                  // browser = server - 4000
  const serverNow = clock.t + 4000;
  await x.agent.message(offer(serverNow));
  clock.t += 29000;                                             // 29 s later the execute arrives with 1 s left
  await x.agent.message(execute(clock.t + 4000, 1000));
  assert.equal(x.messages.at(-1).state, 'completed');
  clock.t += 899;
  assert.doesNotThrow(() => seen[0]());                          // 100 ms safety margin + 899 ms < 1000 ms
  clock.t += 2;
  assert.throws(() => seen[0](), error => error.code === 'command_start_expired');
});

test('the start deadline also bounds the loader entry INSIDE execute (the M2 failure mode, no clock error involved)', async () => {
  const clock = { t: 0 };
  const x = setup(clock, async (command, args, ctx) => {
    ctx.check();                                                // start check before the port is touched: still inside the window
    clock.t += 30715;                                           // a loader sync that keeps reading but never succeeds
    ctx.check();                                                // next checkpoint: the 30 s start window is gone
    return { ok: true };
  });
  x.agent.serverOffset = 0;
  await x.agent.message(offer(0));
  await x.agent.message(execute(0, 29958));
  const last = x.messages.at(-1);
  assert.equal(last.state, 'failed');
  assert.equal(last.error.code, 'command_start_expired');
  assert.equal(last.error.start_remaining_ms, 0);
  assert.ok(last.error.elapsed_ms >= 30000 && last.error.execution_remaining_ms > 0, 'the execution deadline (120 s) and the lease were still alive');
});

test('a suspension between offer and execute fails closed (browser clock ahead of the server clock)', async () => {
  for (const jump of [31000, 60000, 3600000]) {
    const clock = { t: 5000 };
    let runs = 0;
    const x = setup(clock, async () => { runs++; return {}; });
    x.agent.serverOffset = 1000;
    await x.agent.message(offer(clock.t + 1000));
    clock.t += jump;                                            // the page was suspended: performance.now() moved on, the server's timeline did not
    await x.agent.message(execute(5000 + 1000 + 29000, 29000));  // the server still believes 29 s remain
    assert.equal(runs, 0, `a ${jump} ms jump must not start hardware`);
    assert.equal(x.messages.at(-1).state, 'cancelled');
    assert.equal(x.messages.at(-1).error.code, 'expired_before_execution');
  }
});

test('a server clock that runs AHEAD of the browser clock cannot be detected by the agent: the extension is bounded by the server', async () => {
  // Documented limit, not a defect fixed here: if the server's monotonic clock advances MORE than the browser's after the welcome
  // (the opposite of a browser suspension), the agent believes it has more time than the server grants. The server still enforces
  // every deadline itself (core.finish / sweep reject late results and expired leases), so hardware effects stay bounded by it.
  const clock = { t: 0 };
  let runs = 0;
  const x = setup(clock, async () => { runs++; return {}; });
  x.agent.serverOffset = 0;
  await x.agent.message(offer(0));
  await x.agent.message(execute(45000, 1000));                  // server clock +45 s drift: the agent still runs (and the server will refuse the late result)
  assert.equal(runs, 1);
});

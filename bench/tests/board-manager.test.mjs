import test from 'node:test';
import assert from 'node:assert/strict';
import { BoardManager } from '../web/board-manager.js';

function lab() {
  const ports = [{ name: 'A' }, { name: 'B' }];
  const actions = [];
  const agents = [];
  const holds = new Map();
  const manager = new BoardManager({
    allowed: ['a', 'b'], ports: async () => ports,
    createEngine(ctx, port, event) {
      const engine = {
        port, busy: false, hold: holds.get(ctx.id) || false, mode: 'console',
        async identity() { actions.push('identity:' + ctx.id); return { chip: 'esp32s3', flash_size: 4194304, flash_id: ctx.id }; },
        async openConsole() { actions.push('open:' + ctx.id); },
        async close() { actions.push('close:' + ctx.id); },
        capabilities() { return ['identify']; },
        persistHold(value) { holds.set(ctx.id, value); },
        setHold(value) { this.hold = value; this.persistHold(value); },
        cancelTrigger() { actions.push('disarm:' + ctx.id); },
        emit: event,
      };
      return engine;
    },
    createAgent(ctx) {
      const agent = {
        generation: 'gen-' + ctx.id, active: false, awaitingResult: null,
        connect() { actions.push('connect:' + ctx.id); },
        disconnect() { actions.push('disconnect:' + ctx.id); },
        cancelTrigger() {}, grant() {}, event(type, data) { actions.push('event:' + ctx.id + ':' + type); },
      };
      agents.push(agent);
      return agent;
    },
    onEvent(ctx, type) { actions.push('ui:' + ctx.id + ':' + type); },
  });
  return { manager, ports, actions, agents };
}

test('two simulated serial ports and agents stay connected through A-B-A selection', async () => {
  const { manager, ports, actions } = lab();
  const a = await manager.add('a', ports[0]), b = await manager.add('b', ports[1]);
  const before = [...actions];
  manager.select('b'); manager.select('a');
  assert.deepEqual(actions, before);
  assert.equal(a.status, 'connected'); assert.equal(b.status, 'connected');
  assert.notStrictEqual(a.engine, b.engine);
  assert.notStrictEqual(a.agent, b.agent);
  assert.equal(a.engine.port, ports[0]); assert.equal(b.engine.port, ports[1]);
  a.engine.emit('uart', { data: 'QQ==' });
  b.engine.emit('uart', { data: 'Qg==' });
  assert.ok(actions.includes('event:a:uart'));
  assert.ok(actions.includes('event:b:uart'));
  assert.ok(actions.includes('ui:a:uart'));
  assert.ok(actions.includes('ui:b:uart'));
});

test('disconnect A never touches B and never schedules automatic reconnect', async () => {
  const { manager, ports, actions } = lab();
  await manager.add('a', ports[0]); await manager.add('b', ports[1]);
  await manager.disconnect('a');
  assert.equal(manager.get('a').status, 'disconnected');
  assert.equal(manager.get('b').status, 'connected');
  assert.equal(manager.get('a').reconnect.waiting, false);
  assert.equal(actions.includes('disconnect:b'), false);
  assert.equal(actions.includes('close:b'), false);
  assert.ok(actions.includes('close:a'));
});

test('USB loss of A isolates B and preserves A as an offline context', async () => {
  const { manager, ports, actions } = lab();
  const a = await manager.add('a', ports[0]), b = await manager.add('b', ports[1]);
  const unrelated = { name: 'C' };
  assert.equal(manager.lost(unrelated), null);
  manager.lost(ports[0]);
  assert.equal(a.status, 'usb_lost');
  assert.equal(a.reconnect.waiting, true);
  assert.equal(b.status, 'connected');
  assert.equal(actions.includes('disconnect:b'), false);
  manager.cancelReconnect('a');
  assert.equal(a.status, 'disconnected');
  assert.equal(b.status, 'connected');
});

test('operation active on A refuses disconnection while B stays selectable', async () => {
  const { manager, ports } = lab();
  const a = await manager.add('a', ports[0]); await manager.add('b', ports[1]);
  a.agent.active = { id: 'operation' };
  await assert.rejects(manager.disconnect('a'), /board_not_idle/);
  manager.select('b');
  assert.equal(manager.get('a').status, 'connected');
  assert.equal(manager.get('b').status, 'connected');
  a.agent.active = false;
  await manager.disconnect('a');
});

test('manual reconnect uses the exact port and validates physical identity', async () => {
  const { manager, ports } = lab();
  await manager.add('a', ports[0]); await manager.add('b', ports[1]);
  await manager.disconnect('a');
  await manager.reconnect('a');
  assert.equal(manager.get('a').status, 'connected');
  assert.equal(manager.get('b').status, 'connected');
  await manager.disconnect('a');
  ports.splice(0, 1);
  await assert.rejects(manager.reconnect('a'), /serial_port_not_granted/);
});

test('initial invalid ID and duplicate port never create a second engine', async () => {
  const { manager, ports, actions } = lab();
  await manager.add('a', ports[0]);
  const before = [...actions];
  await assert.rejects(manager.add('b', ports[0]), /serial_port_in_use/);
  await assert.rejects(manager.add('bad', ports[1]), /device_forbidden/);
  assert.deepEqual(actions, before);
});

test('automatic reconnect only reuses the previously owned port', async () => {
  const { manager, ports, actions } = lab();
  await manager.add('a', ports[0]); await manager.add('b', ports[1]);
  manager.lost(ports[0]);
  const a = manager.get('a'), b = manager.get('b');
  // The reconnect scheduler is deliberately bypassed: exercise the same
  // attempt path with a deterministic version, no physical serial hardware.
  await a.reconnect.attempt(a.reconnect.version);
  assert.equal(a.status, 'connected');
  assert.equal(a.engine.port, ports[0]);
  assert.equal(b.engine.port, ports[1]);
  assert.ok(actions.filter(s => s === 'identity:a').length >= 2);
  assert.equal(actions.filter(s => s === 'identity:b').length, 1);
  manager.cancelReconnect('a');
});

test('physical identity mismatch never substitutes another permitted port', async () => {
  const { manager, ports } = lab();
  const a = await manager.add('a', ports[0]);
  await manager.add('b', ports[1]);
  manager.lost(ports[0]);
  a.identity.flash_id = 'wrong-card';
  await a.reconnect.attempt(a.reconnect.version);
  assert.notEqual(a.status, 'connected');
  assert.equal(a.reconnect.waiting, false);
  assert.equal(manager.get('b').status, 'connected');
  assert.equal(manager.get('b').engine.port, ports[1]);
});

test('USB loss after a result is pending preserves hold but permits a new generation', async () => {
  const { manager, ports } = lab();
  const a = await manager.add('a', ports[0]);
  await manager.add('b', ports[1]);
  let activity = 0;
  a.agent.activity = () => { activity++; };
  a.agent.awaitingResult = { id: 'old-operation', generation: 'old-generation' };

  assert.equal(manager.lost(ports[0]), a);
  assert.equal(a.engine.hold, true);
  assert.equal(a.agent.orphanedOperation, 'old-operation');
  assert.equal(a.agent.awaitingResult, null);
  assert.equal(activity, 1);
  assert.equal(a.reconnect.waiting, true);
  assert.equal(manager.get('b').status, 'connected');
  await a.reconnect.attempt(a.reconnect.version);
  assert.equal(a.status, 'connected');
  assert.equal(a.engine.hold, true); // The new engine restores the durable hold.
  assert.equal(a.engine.port, ports[0]);
  assert.equal(manager.get('b').engine.port, ports[1]);
});

test('stale callbacks from replaced engine cannot forward UART to new agent', async () => {
  const { manager, ports, actions } = lab();
  const a = await manager.add('a', ports[0]);
  await manager.add('b', ports[1]);
  const oldEngine = a.engine;
  const oldAgent = a.agent;
  manager.lost(ports[0]);
  const oldEventCount = actions.filter(x => x === 'event:a:uart').length;
  oldEngine.emit('uart', { data: 'stale-after-loss' });
  assert.equal(actions.filter(x => x === 'event:a:uart').length, oldEventCount);
  await a.reconnect.attempt(a.reconnect.version);
  assert.notStrictEqual(a.engine, oldEngine);
  assert.notStrictEqual(a.agent, oldAgent);
  oldEngine.emit('uart', { data: 'stale-after-reconnect' });
  assert.equal(actions.filter(x => x === 'event:a:uart').length, oldEventCount);
  a.engine.emit('uart', { data: 'fresh' });
  assert.equal(actions.filter(x => x === 'event:a:uart').length, oldEventCount + 1);
  assert.equal(manager.get('b').status, 'connected');
});

test('late result after physical loss cannot deadlock reconnection or lose hold', async () => {
  const { manager, ports } = lab();
  const a = await manager.add('a', ports[0]);
  await manager.add('b', ports[1]);
  const previousAgent = a.agent;
  previousAgent.activity = () => {};
  previousAgent.disconnect = () => { previousAgent.stopped = true; };
  previousAgent.active = { id: 'executing-when-unplugged' };
  manager.lost(ports[0]);
  assert.equal(a.engine.hold, true);
  assert.equal(a.reconnect.waiting, true);
  // Completion is delivered after the USB loss, but cannot be acknowledged on
  // the disconnected generation. The new generation must start in hold.
  previousAgent.active = null;
  previousAgent.awaitingResult = { id: 'late-result', generation: 'old' };
  a.engine.busy = false;
  await a.reconnect.attempt(a.reconnect.version);
  assert.equal(previousAgent.orphanedOperation, 'late-result');
  assert.equal(previousAgent.awaitingResult, null);
  assert.equal(a.status, 'connected');
  assert.equal(a.engine.hold, true);
  assert.equal(manager.get('b').status, 'connected');
});

test('forget after cancelled physical reconnect closes leftover engine before freeing port', async () => {
  const { manager, ports, actions } = lab();
  await manager.add('a', ports[0]);
  const b = await manager.add('b', ports[1]);
  manager.lost(ports[0]);
  manager.cancelReconnect('a');
  assert.equal(manager.get('a').status, 'disconnected');
  const previousAgent = manager.get('a').agent;
  previousAgent.stopped = true;
  previousAgent.activity = () => {};
  previousAgent.awaitingResult = { id: 'late-after-cancel' };
  await manager.forget('a');
  assert.equal(previousAgent.orphanedOperation, 'late-after-cancel');
  assert.equal(previousAgent.awaitingResult, null);
  assert.equal(manager.get('a'), null);
  assert.equal(manager.get('b'), b);
  assert.ok(actions.includes('close:a'));
  assert.equal(actions.includes('close:b'), false);
  await manager.add('a', ports[0]);
});

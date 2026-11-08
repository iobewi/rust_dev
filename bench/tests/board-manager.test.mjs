import test from 'node:test';
import assert from 'node:assert/strict';
import { BoardManager } from '../web/board-manager.js';

function lab() {
  const ports = [{ name: 'A' }, { name: 'B' }];
  const actions = [];
  const agents = [];
  const manager = new BoardManager({
    allowed: ['a', 'b'], ports: async () => ports,
    createEngine(ctx, port, event) {
      const engine = {
        port, busy: false, hold: false, mode: 'console',
        async identity() { actions.push('identity:' + ctx.id); return { chip: 'esp32s3', flash_size: 4194304, flash_id: ctx.id }; },
        async openConsole() { actions.push('open:' + ctx.id); },
        async close() { actions.push('close:' + ctx.id); },
        capabilities() { return ['identify']; },
        setHold(value) { this.hold = value; },
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

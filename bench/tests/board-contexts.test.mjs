import test from 'node:test';
import assert from 'node:assert/strict';
import { BoardContexts } from '../web/board-contexts.js';

const setup = () => new BoardContexts({ allowed: ['board-a', 'board-b', 'board-c'] });
const port = () => ({ readable: null, writable: null });

test('two ports and agents coexist; selection does not touch hardware', () => {
  const boards = setup(), a = port(), b = port();
  const calls = [];
  const A = boards.add('board-a', a, () => ({ engine: { busy: false, close() { calls.push('close A'); } }, agent: { disconnect() { calls.push('agent A'); } } }));
  const B = boards.add('board-b', b, () => ({ engine: { busy: false, close() { calls.push('close B'); } }, agent: { disconnect() { calls.push('agent B'); } } }));
  boards.markConnected('board-a', { chip: 'esp32s3' }, 'gen-a');
  boards.markConnected('board-b', { chip: 'esp32s3' }, 'gen-b');
  assert.strictEqual(boards.select('board-b'), B);
  assert.strictEqual(boards.select('board-a'), A);
  assert.strictEqual(boards.get('board-b').port, b);
  assert.strictEqual(boards.get('board-b').agent, B.agent);
  assert.deepEqual(calls, []);
});

test('never accept duplicate port, logical ID or unauthorized board', () => {
  const boards = setup(), a = port();
  boards.add('board-a', a);
  assert.throws(() => boards.add('board-b', a), /serial_port_in_use/);
  assert.throws(() => boards.add('board-a', port()), /device_already_added/);
  assert.throws(() => boards.add('foreign', port()), /device_forbidden/);
  assert.equal(boards.list().length, 1);
});

test('a failed initializer releases only its own reservation', () => {
  const boards = setup(), a = port(), b = port();
  boards.add('board-a', a);
  assert.throws(() => boards.add('board-b', b, () => { throw Error('create_failed'); }), /create_failed/);
  assert.equal(boards.list().length, 1);
  boards.add('board-b', b);
  assert.equal(boards.list().length, 2);
});

test('USB loss and voluntary disconnect only change the addressed context', () => {
  const boards = setup(), a = port(), b = port();
  boards.add('board-a', a);
  boards.add('board-b', b);
  boards.markConnected('board-a', { chip: 'esp32s3' }, 'gen-a');
  boards.markConnected('board-b', { chip: 'esp32s3' }, 'gen-b');
  boards.select('board-b');
  assert.equal(boards.lost(a).id, 'board-a');
  assert.equal(boards.get('board-a').status, 'usb_lost');
  assert.equal(boards.get('board-b').status, 'connected');
  assert.equal(boards.get('board-b').generation, 'gen-b');
  assert.equal(boards.selectedId, 'board-b');
  assert.equal(boards.lost(port()), null);
  boards.disconnected('board-a');
  assert.equal(boards.get('board-a').status, 'disconnected');
  assert.equal(boards.lost(a), null);
  assert.equal(boards.get('board-b').status, 'connected');
});

test('operation target snapshot does not change when another board is selected', () => {
  const boards = setup();
  boards.add('board-a', port());
  boards.add('board-b', port());
  boards.markConnected('board-a', {}, 'gen-a');
  boards.markConnected('board-b', {}, 'gen-b');
  const target = boards.target();
  boards.select('board-b');
  assert.equal(target.device, 'board-a');
  assert.equal(target.generation, 'gen-a');
  assert.equal(target.context.id, 'board-a');
  assert.equal(boards.target().device, 'board-b');
  assert.throws(() => boards.disconnected('board-b') && boards.target(), /device_unavailable/);
});

test('forget requires completed safe disconnect and preserves other contexts', () => {
  const boards = setup(), a = port(), b = port();
  boards.add('board-a', a);
  boards.add('board-b', b);
  assert.throws(() => boards.forget('board-a'), /board_not_idle/);
  boards.disconnected('board-a');
  boards.get('board-a').agent = { awaitingResult: { id: 'op' } };
  assert.throws(() => boards.forget('board-a'), /board_not_idle/);
  boards.get('board-a').agent = null;
  boards.get('board-a').reconnect = { waiting: false, running: false, forget() {} };
  boards.forget('board-a');
  assert.equal(boards.get('board-a'), null);
  assert.equal(boards.get('board-b').port, b);
  boards.add('board-a', a);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {classifyMd5Answer, judgeSessionState, observeSessionState, InvalidSyncValue, SYNC_OPCODE, SYNC_FRAME} from '../web/p4-session-state.js';

const ROM = new Array(8).fill(1), STUB = [0];
const judge = (o) => judgeSessionState({isStub: false, syncCompleted: true, syncStubDetected: false, syncValues: ROM, ...o}).verdict;

test('MD5 answer shapes: 32 lowercase hex ASCII bytes = ROM, 16 raw bytes = stub, anything else unknown', () => {
  assert.equal(classifyMd5Answer(Uint8Array.from('d41d8cd98f00b204e9800998ecf8427e', c => c.charCodeAt(0))), 'rom');
  assert.equal(classifyMd5Answer(new Uint8Array(16)), 'stub');
  for (const x of [new Uint8Array(0), new Uint8Array(31), new Uint8Array(32).fill(0xff), 'x', null, new Uint8Array(33)])
    assert.equal(classifyMd5Answer(x), 'unknown');
});

test('verdict table: ROM, stub, contradictions and missing evidence', () => {
  assert.equal(judge({}), 'rom');
  assert.equal(judge({syncValues: STUB, syncCompleted: false, syncStubDetected: false}), 'stub');   // sync() throws on a stub
  assert.equal(judge({syncValues: STUB, syncStubDetected: true}), 'stub');
  assert.equal(judge({syncValues: new Array(8).fill(0), syncStubDetected: true, isStub: true}), 'stub');
  for (const o of [{syncValues: STUB}, {syncStubDetected: true}, {isStub: true}, {syncValues: [1, 1, 0, 1, 1, 1, 1, 1]},
      {syncValues: ROM.slice(0, 7)}, {syncValues: [1]}, {syncValues: []}, {syncValues: undefined}, {syncCompleted: false},
      {syncValues: [0, 0, 0]}, {syncValues: [...ROM.slice(0, 7), 1.5]}, {syncValues: [...ROM.slice(0, 7), NaN]}, {md5: 'stub'},
      {syncValues: STUB, syncStubDetected: true, md5: 'rom'}])
    assert.equal(judge(o), 'indeterminate', JSON.stringify(o));
  assert.equal(judge({md5: 'rom'}), 'rom');
  assert.equal(judge({md5: 'unknown'}), 'indeterminate', 'an md5 probe that cannot tell forbids a positive ROM verdict');
  assert.equal(judge({syncValues: STUB, syncStubDetected: true, md5: 'unknown'}), 'stub');
});

function esptoolLike({answers, syncThrows = false, firstThrows = false}) {
  const calls = []; let pending = [];
  const loader = {IS_STUB: false, syncStubDetected: false, transport: {flushInput() { calls.push('flush'); }},
    async sync() { calls.push('sync'); if (syncThrows) throw new Error('invalid response'); },
    async command(op, data, chk, wait, timeout) { calls.push(['command', op, data.length, chk, wait, timeout]); if (firstThrows) throw new Error('lost'); pending = [...answers]; return [pending.shift(), new Uint8Array(0)]; },
    async readPacket(op, timeout) { calls.push(['readPacket', op, timeout]); if (!pending.length) throw new Error('no data'); return [pending.shift(), new Uint8Array(0)]; }};
  return {loader, calls};
}

test('observing uses the explicit sync opcode 0x08 (loader.ESP_SYNC does not exist), one command then up to seven reads, and leaves nothing pending', async () => {
  const {loader, calls} = esptoolLike({answers: [7, 8, 9, 10, 11, 12, 13, 14]});
  assert.equal(loader.ESP_SYNC, undefined);
  const e = await observeSessionState(loader);
  assert.deepEqual(e.syncValues, [7, 8, 9, 10, 11, 12, 13, 14]); assert.equal(e.syncCompleted, true);
  const commands = calls.filter(c => Array.isArray(c) && c[0] === 'command');
  assert.deepEqual(commands, [['command', SYNC_OPCODE, SYNC_FRAME.length, 0, true, 100]]);
  assert.equal(calls.filter(c => Array.isArray(c) && c[0] === 'readPacket').length, 7);
  assert.equal(calls.at(-1), 'flush');
});

test('a stub answers once: the exchange is kept as one value, the incomplete sync() is recorded, nothing is invented', async () => {
  const {loader} = esptoolLike({answers: [0], syncThrows: true});
  const e = await observeSessionState(loader);
  assert.deepEqual([e.syncValues, e.syncCompleted], [[0], false]);
});

test('signed 32-bit values are kept as received; invalid or missing first answers are failures, never zeros', async () => {
  assert.deepEqual((await observeSessionState(esptoolLike({answers: [-5, 1, 1, 1, 1, 1, 1, 1]}).loader)).syncValues, [-5, 1, 1, 1, 1, 1, 1, 1]);
  for (const answers of [[undefined], [NaN], [2 ** 32], [1, 1, undefined], [1, 1.5]])
    await assert.rejects(observeSessionState(esptoolLike({answers}).loader), e => e instanceof InvalidSyncValue, JSON.stringify(answers));
  await assert.rejects(observeSessionState(esptoolLike({answers: [1], firstThrows: true}).loader), /lost/);
});

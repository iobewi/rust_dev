import test from 'node:test';
import assert from 'node:assert/strict';
import {classifyMd5Answer, judgeSessionState, observeSessionState, SYNC_SAMPLES} from '../web/p4-session-state.js';

const ROM = new Array(8).fill(1), ZEROS = new Array(8).fill(0);
const judge = (o) => judgeSessionState({isStub: false, syncStubDetected: false, syncValues: ROM, ...o}).verdict;

test('MD5 answer shapes: 32 lowercase hex ASCII bytes = ROM, 16 raw bytes = stub, anything else unknown', () => {
  assert.equal(classifyMd5Answer(Uint8Array.from('d41d8cd98f00b204e9800998ecf8427e', c => c.charCodeAt(0))), 'rom');
  assert.equal(classifyMd5Answer(new Uint8Array(16)), 'stub');
  for (const x of [new Uint8Array(0), new Uint8Array(31), new Uint8Array(32).fill(0xff), 'x', null, new Uint8Array(33)])
    assert.equal(classifyMd5Answer(x), 'unknown');
});

test('verdict table: ROM, stub, contradictions and missing evidence', () => {
  assert.equal(judge({}), 'rom');
  assert.equal(judge({syncValues: ZEROS, syncStubDetected: true}), 'stub');
  assert.equal(judge({syncValues: ZEROS, syncStubDetected: true, isStub: true}), 'stub');
  for (const o of [{syncValues: ZEROS}, {syncStubDetected: true}, {isStub: true}, {syncValues: [1, 1, 0, 1, 1, 1, 1, 1]},
      {syncValues: ROM.slice(0, 7)}, {syncValues: undefined}, {syncStubDetected: undefined}, {md5: 'stub'},
      {syncValues: ZEROS, syncStubDetected: true, md5: 'rom'}])
    assert.equal(judge(o), 'indeterminate', JSON.stringify(o));
  assert.equal(judge({md5: 'rom'}), 'rom'); assert.equal(judge({md5: 'unknown'}), 'rom');
});

test('observing sends only sync frames (one sync + eight raw exchanges) and reads the flags afterwards', async () => {
  const calls = [], loader = {IS_STUB: false, ESP_SYNC: 8, async sync() { calls.push('sync'); this.syncStubDetected = false; },
    async command(op, data, chk, wait, timeout) { calls.push(['command', op, data.length, timeout]); return [7, new Uint8Array(0)]; }};
  const e = await observeSessionState(loader);
  assert.equal(e.syncValues.length, SYNC_SAMPLES); assert.deepEqual(e.syncValues, new Array(8).fill(7));
  assert.equal(calls.length, 9); assert.ok(calls.slice(1).every(c => c[1] === 8 && c[2] === 36));
});

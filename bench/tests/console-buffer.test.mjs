import test from 'node:test';
import assert from 'node:assert/strict';
import { ConsoleBuffer } from '../web/console-buffer.js';
import { address, flashPlan, requireTarget } from '../web/ui-model.js';

test('clear/filter/hex presentation never changes raw captured binary bytes', () => {
  const capture = new ConsoleBuffer(), bytes = new Uint8Array([0, 255, 13, 10]);
  capture.append(bytes); bytes[0] = 1;
  assert.equal(capture.visible({ format: 'hex' })[0].display, '00 ff 0d 0a');
  assert.equal(capture.visible({ query: 'missing' }).length, 0);
  capture.clearDisplay(); assert.equal(capture.visible().length, 0);
  capture.append(new Uint8Array([42]));
  assert.deepEqual(capture.bytes(), new Uint8Array([0, 255, 13, 10, 42]));
  assert.equal(capture.visible().length, 1);
});
test('split UTF-8 characters are decoded continuously and the raw bytes remain exact', () => {
  const capture = new ConsoleBuffer(); capture.append(new Uint8Array([0xc3])); capture.append(new Uint8Array([0xa9]));
  assert.equal(capture.visible().map(event => event.display).join(''), 'é');
  assert.deepEqual(capture.bytes(), new Uint8Array([0xc3, 0xa9]));
});
test('capture retention is bounded and eviction is explicit even for oversized frames or marker floods', () => {
  const capture = new ConsoleBuffer({ maxBytes: 4, maxEvents: 3 });
  capture.append(new Uint8Array([1, 2, 3, 4, 5, 6]));
  assert.equal(capture.droppedBytes, 2); assert.deepEqual(capture.bytes(), new Uint8Array([3, 4, 5, 6]));
  for (let i = 0; i < 10; i++) capture.note('REP', 'mark');
  assert.equal(capture.events.length, 3); assert.equal(capture.byteLength, 0); assert.equal(capture.droppedBytes, 6);
  assert.equal(capture.droppedEvents, 8);
});
test('UI numeric inputs refuse blanks, negative values, partial numbers and precision loss', () => {
  assert.equal(address('0x20000'), 131072); assert.equal(address('64'), 64);
  for (const bad of ['', ' ', '-1', '0x', '10abc', '1.5', '1e3', '9007199254740993', 'Infinity']) assert.throws(() => address(bad));
});
test('flash confirmation uses verified artifacts and rejects shared erase sectors before submission', () => {
  const entry = { artifact: 'a', sha256: 'a'.repeat(64), address: '0x1000', size: 64 };
  assert.deepEqual(flashPlan([entry], 65536), [{ artifact: 'a', sha256: 'a'.repeat(64), address: 4096 }]);
  assert.throws(() => flashPlan([{ ...entry, address: '' }], 65536));
  assert.throws(() => flashPlan([entry, { ...entry, address: '0x1040' }], 65536));
  assert.throws(() => flashPlan([{ ...entry, sha256: 'bad' }], 65536));
  assert.throws(() => flashPlan([{ ...entry, address: '0xffff' }], 65536));
});
test('manual control remains bound to the reserved device even for same-family targets', () => {
  const lease = { device: 'board-a', chip: 'esp32s3' };
  assert.equal(requireTarget(lease, 'board-a'), lease);
  assert.throws(() => requireTarget(lease, 'board-b')); assert.throws(() => requireTarget(null, 'board-a'));
});

test('a paused presentation can keep a fixed cursor while capture and exports continue', () => {
  const capture = new ConsoleBuffer(); capture.append(new Uint8Array([65])); const pausedAt = capture.sequence;
  capture.append(new Uint8Array([66]));
  assert.equal(capture.visible({ before: pausedAt }).map(event => event.display).join(''), 'A');
  assert.equal(capture.visible().map(event => event.display).join(''), 'AB');
  assert.deepEqual(capture.bytes(), new Uint8Array([65, 66]));
});

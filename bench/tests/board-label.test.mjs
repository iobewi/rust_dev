import test from 'node:test';
import assert from 'node:assert/strict';
import { displayChip } from '../web/ui-model.js';

test('active board label prefers verified local identity', () => {
  assert.equal(displayChip({ chip: 'esp32s3' }, { chip: 'esp32c6' }), 'esp32s3');
});

test('disconnected board displays known server chip', () => {
  assert.equal(displayChip(null, { chip: 'esp32c6', connected: false }), 'esp32c6');
});

test('board with neither identity displays unknown', () => {
  assert.equal(displayChip(null, null), 'MCU inconnu');
  assert.equal(displayChip({}, {}), 'MCU inconnu');
});

test('server identity is used for display only and is not mutated', () => {
  const server = Object.freeze({ chip: 'esp32c6', connected: false });
  assert.equal(displayChip(null, server), 'esp32c6');
  assert.equal(server.connected, false);
});

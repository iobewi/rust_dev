// The target of a manual command: board identity is separate from the flash capacity (ROM seule, flash_size null).
// ui.js needs a DOM and a browser (not available here): the decision logic lives in ui-model.js and is tested directly with the
// device views the server really returns; the wiring of ui.js is checked statically. Rendering/interaction stay to Playwright / the operator.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { CAPACITY_COMMANDS, targetFor, connectedTarget, requireKnownCapacity } from '../web/ui-model.js';
import { P4_TRACE } from '../web/p4-diagnostic.js';

const ui = readFileSync(new URL('../web/ui.js', import.meta.url), 'utf8');
// what GET /devices returns for a ROM-only P4 whose capacity is unknown, and for a normally connected board
const unknown = { id: 'esp32-local', chip: 'esp32p4', connected: true, generation: 'g1', flash_size: null, flash_size_known: false, capabilities: ['identify', 'mark', P4_TRACE] };
const known = { id: 'esp32-zero', chip: 'esp32c6', connected: true, generation: 'g2', flash_size: 8388608, flash_size_known: true, capabilities: ['identify', 'flash.read'] };
const ctx = { session: { devices: ['esp32-local'] }, board: { status: 'connected' } };

test('submit(P4_TRACE / identify / mark) on a connected board with an unknown capacity resolves its target', () => {
  for (const command of [P4_TRACE, 'identify', 'mark']) {
    const target = targetFor(unknown, ctx, command);
    assert.deepEqual(target, { device: 'esp32-local', chip: 'esp32p4', flash_size: null, generation: 'g1' }, command);
  }
  assert.deepEqual(connectedTarget(unknown, ctx), targetFor(unknown, ctx));
});

test('the capacity is demanded by exactly the commands that depend on it, and by image preparation / erase / write handlers', () => {
  assert.deepEqual([...CAPACITY_COMMANDS].sort(), ['flash.erase', 'flash.read', 'flash.write', 'partitions']);
  for (const command of CAPACITY_COMMANDS) {
    assert.throws(() => targetFor(unknown, ctx, command), /Capacité flash inconnue/, command);
    assert.equal(targetFor(known, { session: {}, board: null }, command).flash_size, 8388608, command);
  }
  assert.throws(() => targetFor(unknown, ctx, true), /Capacité flash inconnue/);        // validatePlan / confirm-write / erase / manifest
  for (const bad of [{ ...unknown, flash_size: 0 }, { ...unknown, flash_size: '4194304' }, { ...unknown, flash_size: 1.5 }, { ...unknown, flash_size: undefined }])
    assert.throws(() => targetFor(bad, ctx, 'flash.read'), /Capacité flash inconnue/);
  assert.throws(() => requireKnownCapacity({ flash_size: null }));
});

test('no regression on the identity checks: no session, not connected, board not connected, unknown device', () => {
  assert.throws(() => targetFor(unknown, { session: null, board: null }, P4_TRACE), /Connectez une carte/);
  assert.throws(() => targetFor({ ...unknown, connected: false }, ctx, P4_TRACE), /Connectez une carte/);
  assert.throws(() => targetFor(unknown, { session: {}, board: { status: 'recovery_required' } }, P4_TRACE), /Connectez une carte/);
  assert.throws(() => targetFor(undefined, ctx, 'identify'), /Connectez une carte/);
  assert.equal(targetFor(known, { session: {}, board: undefined }, 'flash.read').generation, 'g2');
});

test('ui.js wiring: submit() asks for the capacity per command, every flash-dependent handler asks for it, the diagnostic goes through submit()', () => {
  assert.match(ui, /const target = selectedTarget\(command\);/);
  assert.match(ui, /function selectedTarget\(capacityNeeded = false\) \{\s*const d = info\(\);\s*return targetFor\(d, \{ session, board: d && boards\.get\(d\.id\) \}, capacityNeeded\);/);
  assert.equal((ui.match(/selectedTarget\(true\)/g) || []).length, 4, 'validatePlan, confirm-write, erase, manifest');
  assert.equal((ui.match(/selectedTarget\(\)/g) || []).length, 0, 'no handler resolves a target without saying what it needs');
  assert.match(ui, /await submit\(P4_TRACE, args, true\)/);
  assert.doesNotMatch(ui, /Capacité flash inconnue/, 'the rule lives in ui-model.js, not duplicated in ui.js');
});

test('no classic flash command becomes reachable: every flash handler still resolves its target with the capacity requirement', () => {
  for (const [needle, count] of [["submit('flash.read'", 1], ["submit('partitions'", 1], ["submit('flash.write'", 1], ["submit('flash.erase'", 1]]) {
    const hits = ui.split(needle).length - 1;
    assert.ok(hits >= count, needle);
  }
  // submit() is the only place that posts a command and it computes the requirement from the command name
  assert.equal((ui.match(/manual-operations/g) || []).length >= 1, true);
  for (const command of ['flash.read', 'partitions', 'flash.write', 'flash.erase']) assert.throws(() => targetFor(unknown, ctx, command));
});

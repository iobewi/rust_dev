import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const ui = readFileSync(new URL('../web/ui.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('../web/style.css', import.meta.url), 'utf8');

test('board connection status and action occupy one interactive control', () => {
  assert.match(ui, /status = document\.createElement\('button'\)/);
  assert.match(ui, /status\.className = 'board-status board-connection'/);
  assert.match(ui, /status\.append\(stateText, actionText\)/);
  assert.match(ui, /status\.onclick = safely\(/);
  assert.match(ui, /row\.append\(name, chip, status, grantMarker, quickRevoke\)/);
  assert.doesNotMatch(ui, /row\.append\(name, chip, status, action,/);
});

test('connection swap supports hover, keyboard focus and touch', () => {
  assert.match(css, /\.board-connection:not\(:disabled\):is\(:hover, :focus-visible\) \.board-connection-state/);
  assert.match(css, /\.board-connection:not\(:disabled\):is\(:hover, :focus-visible\) \.board-connection-action/);
  assert.match(css, /@media \(hover:none\)/);
  assert.match(css, /\.board-connection:focus-visible/);
});

test('session recovery remains a separate per-board action', () => {
  assert.match(ui, /recover\.textContent = 'Réinitialiser session'/);
  assert.match(ui, /await boards\.recoverSession\(ctx\.id\)/);
  assert.match(css, /\.board-recover \{ grid-column:2; grid-row:2; \}/);
});

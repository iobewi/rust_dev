import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const ui = readFileSync(new URL('../web/ui.js', import.meta.url), 'utf8');

test('grant actions live directly below identification for the selected card', () => {
  const identification = html.indexOf('id="card-identification-title"');
  const grants = html.indexOf('id="card-permissions"');
  const commands = html.indexOf('id="card-commands"');
  assert.ok(identification >= 0 && identification < grants && grants < commands);
  for (const id of ['grant', 'revoke', 'grant-time', 'grant-state'])
    assert.equal((html.match(new RegExp('id="' + id + '"', 'g')) || []).length, 1);
  assert.doesNotMatch(html, /id="status-permissions"|id="status-panel-permissions"/);
});

test('each active board displays a separate live grant marker', () => {
  assert.match(ui, /grantMarker\.className = 'board-grant'/);
  assert.match(ui, /row\.append\(name, chip, status, grantMarker\)/);
  assert.match(ui, /boards\.get\(row\.dataset\.device\)/);
  assert.match(ui, /ctx\?\.status === 'connected'/);
  assert.match(ui, /ctx\.agent\.socket\?\.readyState === 1/);
  assert.match(ui, /ctx\.agent\.grantUntil > performance\.now\(\)/);
  assert.doesNotMatch(ui, /indicator\('permissions'/);
});

test('grant and revoke use card-specific agent, not global selection alias', () => {
  const start = ui.indexOf("hook('grant',");
  const end = ui.indexOf("hook('release-control',", start);
  assert.ok(start > 0 && end > start);
  const actions = ui.slice(start, end);
  assert.match(actions, /const id = selected\(\), ctx = boards\.get\(id\)/);
  assert.match(actions, /boards\.get\(id\) !== ctx/);
  assert.match(actions, /ctx\.agent\.grant\(duration\)/);
  assert.match(actions, /const ctx = boards\.get\(selected\(\)\)/);
  assert.match(actions, /ctx\.agent\.grant\(0\)/);
  assert.doesNotMatch(actions, /(^|[^.])agent\.grant\(/);
});

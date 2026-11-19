// STATIC checks of the P4 diagnostic panel (markup and wiring). There is no browser in this environment:
// this does NOT validate rendering or interaction, which stay to be checked by Playwright in CI / by the operator.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const ui = readFileSync(new URL('../web/ui.js', import.meta.url), 'utf8');
const panel = html.slice(html.indexOf('<details id="p4-diagnostic"'), html.indexOf('</details>', html.indexOf('<details id="p4-diagnostic"')));

test('the panel is hidden by default, collapsed, and appears once in the tools tab', () => {
  assert.match(panel, /^<details id="p4-diagnostic" class="padded" hidden>/);
  assert.equal((html.match(/id="p4-diagnostic"/g) || []).length, 1);
  assert.ok(html.indexOf('id="panel-tools"') < html.indexOf('id="p4-diagnostic"'));
  assert.doesNotMatch(panel, /<details[^>]* open/);
});

test('it shows what the operator needs to decide: temporary RAM load, range, no NOR write, stub SHA-256, board', () => {
  for (const needle of ['en RAM', '0x8000', '1 048 576', 'aucune écriture NOR', 'id="p4-sha"', 'id="p4-board"', 'non mesuré', 'BOOT + reset', 'récupération locale'])
    assert.ok(panel.replace(/&nbsp;/g, ' ').includes(needle), needle);
});

test('the RAM-load consent is its own clearly worded checkbox, separate from the launch button, and the button starts disabled', () => {
  assert.match(panel, /<input id="p4-consent" type="checkbox">[^<]*chargement et l'exécution de ce code en RAM/);
  assert.match(panel, /<button id="p4-run" data-hardware disabled>/);
  assert.doesNotMatch(panel, /data-command/, 'must not be wired through the generic data-command handler');
});

test('wiring: capability-gated, fixed parameters, consent bound to board+SHA, single use, reset on a new stub', () => {
  assert.match(ui, /'p4-run': P4_TRACE/);
  assert.match(ui, /renderP4Diagnostic\(d, capability\.has\(P4_TRACE\)\)/);
  assert.match(ui, /\$\('p4-diagnostic'\)\.hidden = !offered/);
  assert.match(ui, /consent: expectedConsent\(selected\(\), stub\.sha256\)/);
  assert.match(ui, /submit\(P4_TRACE, args, true\)/);
  const run = ui.slice(ui.indexOf("hook('p4-run'"), ui.indexOf("hook('read'"));
  assert.ok(run.indexOf("$('p4-consent').checked = false") < run.indexOf('submit(P4_TRACE'), 'consent is consumed before submitting');
  const change = ui.slice(ui.indexOf("$('p4-stub-file').addEventListener"), ui.indexOf("hook('p4-run'"));
  assert.match(change, /\$\('p4-consent'\)\.checked = false/);
  assert.match(change, /64 \* 1024/);
  for (const forbidden of ['reader', 'packet-diagnostic']) assert.ok(!run.includes(forbidden), 'not a flash.read reader value: ' + forbidden);
});

test('the launch button is never enabled by the quarantine exception list of standard commands', () => {
  assert.match(ui, /const safeCommands = new Set\(\['identify', 'partitions', 'flash\.read', 'mark'\]\)/);
});

test('the "ROM seule" connection option: advanced, unchecked by default, implies manual entry, reset after use, stored per board', () => {
  const adv = html.slice(html.indexOf('<details class="loader-advanced">'), html.indexOf('</details>', html.indexOf('<details class="loader-advanced">')));
  assert.match(adv, /<input id="rom-only" type="checkbox">/); assert.doesNotMatch(adv, /<input id="rom-only"[^>]* checked/);
  assert.match(adv, /sans stub standard/); assert.equal((html.match(/id="rom-only"/g) || []).length, 1);
  assert.match(ui, /hardware\.romOnly = ctx\.options\?\.romOnly === true/);
  assert.match(ui, /\$\('rom-only'\)\.checked = false/);
  const bm = readFileSync(new URL('../web/board-manager.js', import.meta.url), 'utf8');
  assert.match(bm, /manualDownload: options\.manualDownload === true \|\| options\.romOnly === true, romOnly: options\.romOnly === true/);
});

test('ROM seule visibly forces and locks BOOT manuel (no DTR/RTS reset that would drop the native USB OTG)', () => {
  assert.match(ui, /function syncRomOnly\(\) \{\s*const rom = \$\('rom-only'\), manual = \$\('manual-download'\);\s*if \(rom\.checked\) manual\.checked = true;\s*manual\.disabled = rom\.checked;/);
  assert.match(ui, /\$\('rom-only'\)\.addEventListener\('change', syncRomOnly\)/);
  assert.match(ui, /\$\('rom-only'\)\.checked = false; syncRomOnly\(\);/);
});

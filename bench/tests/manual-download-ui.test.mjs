// Static checks of the « Entrée manuelle » checkbox (markup, CSS and its wiring). There is no browser in the devcontainer: this proves the structure and the scope of the rules,
// NOT the rendering, which is checked by the operator on the real page.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const css = readFileSync(new URL('../web/style.css', import.meta.url), 'utf8');
const ui = readFileSync(new URL('../web/ui.js', import.meta.url), 'utf8');
const block = css.slice(css.indexOf('/* manual-download:start'), css.indexOf('/* manual-download:end */'));

test('the checkbox sits inside its label, which is bound to it with for/id, with the text in a span (the whole label is clickable)', () => {
  const m = html.match(/<label class="manual-download" for="manual-download"><input id="manual-download" type="checkbox"><span>([^<]+)<\/span><\/label>/);
  assert.ok(m, 'label markup'); assert.match(m[1], /[Ee]ntrée manuelle/);
  assert.match(m[1], /carte ajoutée uniquement/); assert.equal((html.match(/id="manual-download"/g) || []).length, 1);
  assert.ok(html.indexOf('add-card-controls') < html.indexOf('id="manual-download"'), 'it lives in the add-card controls');
});

test('the checkbox is captured for the new board; engines use only its own context', () => {
  assert.match(ui, /const manualDownload = \$\('manual-download'\)\?\.checked === true;/);
  assert.match(ui, /boards\.add\(id, port, \{ http_origins, manualDownload, loaderBaud \}\)/);
  assert.match(ui, /hardware\.manualDownload = ctx\.options\?\.manualDownload === true;/);
  assert.match(ui, /\$\('manual-download'\)\.checked = false;/);
  assert.doesNotMatch(ui, /hardware\.manualDownload = \$\('manual-download'\)/);
  assert.ok(!/localStorage[^;]*manual-download/.test(ui));
});

test('the CSS block exists, sizes the box at 18 px that cannot shrink, aligns it with the text and styles focus, hover and disabled', () => {
  assert.ok(block.length > 200, 'block present');
  assert.match(block, /input\[type=checkbox\]\s*\{[^}]*flex:0 0 18px[^}]*width:18px[^}]*height:18px[^}]*min-width:18px[^}]*min-height:18px/);
  assert.match(block, /\.manual-download\s*\{[^}]*display:flex[^}]*align-items:flex-start[^}]*cursor:pointer/);
  assert.match(block, /:focus-visible\s*\{[^}]*outline:2px solid var\(--accent\)/);
  assert.match(block, /:disabled[^{]*\{[^}]*not-allowed/);
  assert.match(block, /\.manual-download:hover/);
});

test('every rule of the block is scoped to .manual-download: no other control of the interface is touched', () => {
  const body = block.replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = [...body.matchAll(/([^{}]+)\{[^}]*\}/g)].map(m => m[1].trim());
  assert.ok(rules.length >= 5);
  for (const selectorList of rules) for (const selector of selectorList.split(',')) assert.match(selector.trim(), /^\.manual-download\b/, selector);
});

test('the block is the only place where this checkbox is styled and the global checkbox rule is untouched', () => {
  const outside = css.replace(block, '').replace('/* manual-download:end */', '');
  assert.ok(!/manual-download/.test(outside));
  assert.ok(outside.includes('input[type=checkbox]{min-height:0;margin:0}'));
});


test('the UART loader speed is captured before USB selection and reapplied only from board context', () => {
  assert.equal((html.match(/id="loader-baud"/g) || []).length, 1);
  assert.ok(ui.indexOf("const loaderBaud = $('loader-baud')") < ui.indexOf('const port = await navigator.serial.requestPort()'));
  assert.match(ui, /hardware\.loaderBaud = ctx\.options\?\.loaderBaud \?\? null/);
  assert.match(ui, /boards\.add\(id, port, \{ http_origins, manualDownload, loaderBaud \}\)/);
});

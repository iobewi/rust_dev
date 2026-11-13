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
  assert.ok(m, 'label markup'); assert.match(m[1], /Entrée manuelle/); assert.equal((html.match(/id="manual-download"/g) || []).length, 1);
  assert.ok(html.indexOf('add-card-controls') < html.indexOf('id="manual-download"'), 'it lives in the add-card controls');
  assert.equal(m[1], 'Entrée manuelle', 'only the compact label stays visible');
  assert.match(html, /class="manual-download-row"/);
  assert.match(html, /aria-describedby="help-manual-download"/);
  assert.match(html, /id="help-manual-download" class="help-popup" role="tooltip"/);
  assert.match(html, /BOOT \+ RESET/);
});

test('the page reads the checkbox when the card engine is created (opt-in per connection, never remembered)', () => {
  assert.match(ui, /hardware\.manualDownload = \$\('manual-download'\)\?\.checked === true;/);
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

test('manual entry help bubble remains keyboard accessible without changing checkbox semantics', () => {
  assert.match(html, /class="help manual-download-help"><button type="button" class="help-trigger" aria-label="Aide : entrée manuelle"/);
  assert.match(block, /\.manual-download-row \.help-popup \{[^}]*right:0/);
  assert.match(block, /\.manual-download-row \.manual-download \{[^}]*align-items:center/);
  assert.equal((html.match(/id="help-manual-download"/g) || []).length, 1);
});

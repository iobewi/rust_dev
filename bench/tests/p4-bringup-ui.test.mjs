// Integration-only regression: the new UI consent is never reusable across generations.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const ui = readFileSync(new URL('../web/ui.js', import.meta.url), 'utf8');
const section = html.slice(html.indexOf('<details id="p4-bringup"'), html.indexOf('</details>', html.indexOf('<details id="p4-bringup"')));

test('bring-up has a separate, initially hidden, disabled and consent-gated control', () => {
  assert.match(section, /^<details id="p4-bringup" class="padded" hidden>/);
  assert.match(section, /id="p4-bringup-consent" type="checkbox"/);
  assert.match(section, /id="p4-bringup-run" data-hardware disabled/);
  assert.match(ui, /renderP4Bringup\(capability\.has\(P4_BRINGUP\)\)/);
});

test('uploaded candidate and explicit consent are scoped to device, generation and SHA', () => {
  assert.match(ui, /function bringupMatches\(stub, target\)/);
  assert.match(ui, /stub\.device === target\.device/);
  assert.match(ui, /stub\.generation === target\.generation/);
  assert.match(ui, /p4BringupConsent\.generation === target\.generation/);
  assert.match(ui, /p4BringupConsent\.sha256 === stub\.sha256/);
  assert.match(ui, /p4Bringup = \{\.\.\.target, name: file\.name, sha256: digest, artifact: artifact\.id\}/);
});

test('asynchronous upload does not bind a firmware to a new selected session', () => {
  const code = ui.slice(ui.indexOf("$('p4-bringup-file').addEventListener"), ui.indexOf("hook('p4-bringup-run'"));
  assert.match(code, /const target = bringupSelection\(\)/);
  assert.match(code, /const currentTarget = bringupSelection\(\)/);
  assert.match(code, /target\.device !== currentTarget\.device \|\| target\.generation !== currentTarget\.generation/);
  assert.ok(code.indexOf('const currentTarget') < code.indexOf('p4Bringup = {...target'));
});

test('submit consumes consent before dispatch and rejects a changed generation', () => {
  const code = ui.slice(ui.indexOf("hook('p4-bringup-run'"), ui.indexOf("hook('read'"));
  assert.match(code, /p4BringupConsent\.generation !== target\.generation/);
  assert.match(code, /p4BringupConsent\.sha256 !== stub\.sha256/);
  assert.ok(code.indexOf('p4BringupConsent = null') < code.indexOf('submit(P4_BRINGUP'));
});

test('only the most recently started upload can publish a candidate', async () => {
  // Deferred upload A finishes after B; the current choice must remain B.
  let seq = 0, candidate = null;
  let finishA, finishB;
  const uploadA = new Promise(resolve => { finishA = resolve; });
  const uploadB = new Promise(resolve => { finishB = resolve; });
  const select = async (name, pending) => {
    const mySeq = ++seq;
    candidate = null;
    const artifact = await pending;
    if (mySeq !== seq) return;
    candidate = {name, artifact};
  };
  const a = select('A.json', uploadA);
  const b = select('B.json', uploadB);
  finishB('b-id');
  await b;
  assert.deepEqual(candidate, {name: 'B.json', artifact: 'b-id'});
  finishA('a-id');
  await a;
  assert.deepEqual(candidate, {name: 'B.json', artifact: 'b-id'});
  // The production UI must apply the same guard immediately before assigning.
  assert.match(ui, /const seq = \+\+p4BringupUploadSeq/);
  assert.match(ui, /if \(seq !== p4BringupUploadSeq\) return;/);
  const handler = ui.slice(ui.indexOf("$('p4-bringup-file').addEventListener"), ui.indexOf("hook('p4-bringup-run'"));
  assert.ok(handler.indexOf('if (seq !== p4BringupUploadSeq) return;') <
    handler.indexOf('p4Bringup = {...target, name: file.name'));
});

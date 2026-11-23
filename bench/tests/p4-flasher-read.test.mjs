// Software-only: bounded read-only plan, opcode allowlist, ack protocol and failure stops. No board, no browser.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {P4_FLASHER_READ, OPCODES, md5Timeout, ReadOnlyFlasher, expectedFlasherConsent, runP4FlasherRead, validateFlasherArgs, validatePlan} from '../web/p4-flasher-read.js';
import {sha256} from '../web/protocol.js';

const stubText = JSON.stringify({entry: 0x4ff50010, text: Buffer.alloc(100, 0x13).toString('base64'), text_start: 0x4ff50000,
  data: Buffer.alloc(40, 7).toString('base64'), data_start: 0x4ff60040, bss_start: 0x4ff60000});
const bytes = new TextEncoder().encode(stubText), sha = await sha256(bytes);
const device = {id: 'p4', chip: 'esp32p4'};
const NOR = Uint8Array.from({length: 1 << 16}, (_, i) => (i * 31 + (i >> 8)) & 255);   // synthetic content, not a flash dump
const md5 = data => createHash('md5').update(data).digest();
const read = value => Array.from(value);

async function argsFor(plan) {
  return {chip: 'esp32p4', stub_artifact: 'artifact', stub_sha256: sha, scenario: 'read_only_v1', plan,
    consent: await expectedFlasherConsent('p4', sha, plan)};
}

// Loader + simulated stub speaking the read-only subset.
function fake({badDigest = false, shortPacket = false, failStatus = false} = {}) {
  const calls = [], writes = [], queue = [];
  const loader = {
    IS_STUB: false, ESP_RAM_BLOCK: 0x1800, DEFAULT_TIMEOUT: 1000, calls, writes, queue, timeouts: [],
    chip: {CHIP_NAME: 'ESP32-P4', USB_RAM_BLOCK: 0x800, getChipRevision: async () => 302},
    usesUsbOtg: async () => true, sync: async () => {}, pending: [],
    command: async () => { loader.pending = Array(7).fill(1); return [1, new Uint8Array(0)]; },
    readPacket: async () => [loader.pending.shift(), new Uint8Array(0)],
    memBegin: async () => {}, memBlock: async () => {}, memFinish: async () => {},
    checkCommand: async () => { calls.push('FORBIDDEN_CHECKCOMMAND'); throw new Error('forbidden'); },
    flashBegin: async () => { calls.push('FORBIDDEN_FLASH'); throw new Error('forbidden'); },
    transport: {
      flushInput() {},
      async read(timeout) {
        loader.timeouts.push(timeout);
        if (!loader.IS_STUB) return Uint8Array.from('OHAI', c => c.charCodeAt(0));
        if (!queue.length) throw new Error('timeout');
        return queue.shift();
      },
      async write(payload) {
        writes.push(read(payload));
        if (payload.length === 4 && payload[1] !== undefined && writes.at(-1).length === 4 && loader.expectAck) return;   // cumulative ack
        const op = payload[1], body = payload.subarray(8), dv = new DataView(body.buffer, body.byteOffset, body.byteLength);
        const reply = (data = []) => Uint8Array.from([1, op, data.length + 2, 0, 0, 0, 0, 0, ...data, failStatus ? 1 : 0, failStatus ? 5 : 0]);
        if (op === 0x0d) queue.push(reply());
        else if (op === 0x13) queue.push(reply(read(md5(NOR.subarray(dv.getUint32(0, true), dv.getUint32(0, true) + dv.getUint32(4, true))))));
        else if (op === 0xd2) {
          const [address, size, packet] = [dv.getUint32(0, true), dv.getUint32(4, true), dv.getUint32(8, true)];
          queue.push(reply());
          for (let at = 0; at < size; at += packet) queue.push(NOR.slice(address + at, address + Math.min(size, at + packet) - (shortPacket && at + packet >= size ? 1 : 0)));
          queue.push(badDigest ? new Uint8Array(16) : md5(NOR.subarray(address, address + size)));
          loader.expectAck = true;
        } else { calls.push('UNEXPECTED_OPCODE_' + op); }
      },
    },
  };
  return loader;
}

test('plan validation: only attach (first), read and md5, bounded', () => {
  validatePlan([{op: 'attach'}, {op: 'read', address: 0, size: 4096, packet_size: 4096}, {op: 'md5', address: 0, size: 4096}]);
  const bad = [
    [], [{op: 'write', address: 0, size: 1}], [{op: 'erase', address: 0, size: 4096}], [{op: 'write_reg'}],
    [{op: 'read', address: 0, size: 1}], [{op: 'read', address: 0, size: 1, packet_size: 4096, extra: 1}],
    [{op: 'read', address: 1 << 25, size: 1, packet_size: 4096}], [{op: 'read', address: (1 << 25) - 1, size: 2, packet_size: 4096}],
    [{op: 'read', address: 0, size: 4096, packet_size: 8192}], [{op: 'read', address: 0, size: 0, packet_size: 4096}],
    [{op: 'read', address: 0, size: 4 * 1024 * 1024 + 1, packet_size: 4096}],
    [{op: 'md5', address: 0, size: 1}, {op: 'attach'}], [{op: 'attach', size: 1}],
    Array(33).fill({op: 'md5', address: 0, size: 1}),
    Array(5).fill({op: 'read', address: 0, size: 4 * 1024 * 1024, packet_size: 4096}),
  ];
  for (const plan of bad) assert.throws(() => validatePlan(plan), {code: 'diagnostic_parameter_not_allowed'}, JSON.stringify(plan).slice(0, 80));
});

test('consent is bound to the exact plan', async () => {
  const plan = [{op: 'md5', address: 0, size: 4096}];
  await validateFlasherArgs(await argsFor(plan), device);
  const other = await argsFor([{op: 'md5', address: 0, size: 8192}]);
  await assert.rejects(validateFlasherArgs({...(await argsFor(plan)), consent: other.consent}, device), {code: 'diagnostic_consent_invalid'});
  await assert.rejects(validateFlasherArgs({...(await argsFor(plan)), extra: 1}, device), {code: 'diagnostic_parameter_not_allowed'});
});

test('opcode allowlist: write, erase, register and parameter commands are refused before any write', async () => {
  const written = [], flasher = new ReadOnlyFlasher({write: async p => written.push(p), read: async () => new Uint8Array()});
  for (const op of [0x02, 0x03, 0x04, 0x09, 0x0a, 0x0b, 0x10, 0x11, 0x12, 0xd0, 0xd1, 0xdf, 0x05, 0x07])
    await assert.rejects(flasher.send(op, new Uint8Array(16)), {code: 'diagnostic_flasher_opcode_not_allowed'}, 'op ' + op);
  assert.equal(written.length, 0);
  assert.deepEqual(Object.values(OPCODES).sort((a, b) => a - b), [0x0d, 0x13, 0xd2]);
});

test('normal run: attach, partial last packet, unaligned read, md5; exact acks; same transport', async () => {
  const loader = fake(), transport = loader.transport;
  const plan = [{op: 'attach'}, {op: 'read', address: 4096, size: 10000, packet_size: 4096},
    {op: 'read', address: 3, size: 5001, packet_size: 1024}, {op: 'md5', address: 0, size: 20000}];
  const result = await runP4FlasherRead({loader, args: await argsFor(plan), device, stubBytes: bytes, romOnly: true});
  assert.equal(loader.transport, transport);
  assert.equal(result.nor_written, false);
  assert.deepEqual(result.opcodes_sent, [0x0d, 0x13, 0xd2]);
  const [attach, big, odd, sum] = result.steps;
  assert.equal(attach.ok, true);
  assert.equal(big.bytes, 10000); assert.equal(big.packets, 3); assert.equal(big.md5_match, true);
  assert.equal(big.data_sha256, createHash('sha256').update(NOR.subarray(4096, 14096)).digest('hex'));
  assert.equal(odd.bytes, 5001); assert.equal(odd.packets, 5); assert.equal(odd.md5_match, true);
  assert.equal(sum.md5_stub, md5(NOR.subarray(0, 20000)).toString('hex'));
  const acks = loader.writes.filter(w => w.length === 4).map(w => w[0] | w[1] << 8);
  assert.deepEqual(acks, [4096, 8192, 10000, 1024, 2048, 3072, 4096, 5001]);
  assert.deepEqual(loader.calls, []);
});

test('failures stop the run, never retry, and stay indeterminate', async () => {
  const plan = [{op: 'read', address: 0, size: 4096, packet_size: 4096}, {op: 'md5', address: 0, size: 4096}];
  for (const [option, code] of [[{badDigest: true}, 'diagnostic_flasher_md5_mismatch'], [{shortPacket: true}, 'diagnostic_flasher_bad_packet'],
    [{failStatus: true}, 'diagnostic_flasher_status_error']]) {
    const loader = fake(option);
    await assert.rejects(runP4FlasherRead({loader, args: await argsFor(plan), device, stubBytes: bytes, romOnly: true}),
      error => { assert.equal(error.code, code); assert.equal(error.indeterminate, true); assert.equal(error.diagnostic.category, 'stream_failed'); return true; });
    assert.equal(loader.writes.filter(w => w[1] === 0x13).length, 0, 'no command after the failure');
    assert.deepEqual(loader.calls, []);
  }
});

test('local refusal: nothing is sent to the chip', async () => {
  const loader = fake(), args = await argsFor([{op: 'md5', address: 0, size: 4096}]);
  args.plan = [{op: 'write', address: 0, size: 1}];
  await assert.rejects(runP4FlasherRead({loader, args, device, stubBytes: bytes, romOnly: true}),
    error => { assert.equal(error.indeterminate, false); assert.equal(error.diagnostic.ram_load, 'not_started'); return true; });
  assert.equal(loader.writes.length, 0);
});

test('command name is exported', () => assert.equal(P4_FLASHER_READ, 'diagnostic.p4_flasher_read'));

test('SPI_FLASH_MD5 silence budget scales with the region; reads keep a per-packet budget; final digest is consumed', async () => {
  const loader = fake();
  const plan = [{op: 'read', address: 0, size: 8192, packet_size: 4096}, {op: 'md5', address: 0, size: 1 << 16}];
  await runP4FlasherRead({loader, args: await argsFor(plan), device, stubBytes: bytes, romOnly: true});
  assert.equal(loader.queue.length, 0, 'every stub frame, including the final 16-byte digest, was consumed');
  assert.ok(loader.timeouts.includes(md5Timeout(1 << 16)));
  assert.equal(md5Timeout(1 << 20), 5000 + 256 * 100);
  assert.ok(md5Timeout(1 << 20) > md5Timeout(4096));
});

test('a digest frame of the wrong size stops the session before any further command', async () => {
  const loader = fake();
  const plan = [{op: 'read', address: 0, size: 4096, packet_size: 4096}, {op: 'md5', address: 0, size: 4096}];
  const write = loader.transport.write;
  loader.transport.write = async payload => { await write(payload); if (payload[1] === 0xd2) loader.queue[loader.queue.length - 1] = new Uint8Array(15); };
  await assert.rejects(runP4FlasherRead({loader, args: await argsFor(plan), device, stubBytes: bytes, romOnly: true}), {code: 'diagnostic_flasher_bad_digest'});
  assert.equal(loader.writes.filter(w => w[1] === 0x13).length, 0);
});

test('static guard: the only writers to the port are send() (allowlisted opcodes) and the cumulative ack', async () => {
  const {readFileSync} = await import('node:fs');
  const source = readFileSync(new URL('../web/p4-flasher-read.js', import.meta.url), 'utf8');
  const writes = source.split('\n').filter(line => /transport\.write\(|checkCommand|flashBegin|flashData|flashFinish|eraseFlash|writeReg|writeRegister|memBegin|memBlock|memFinish|command\(/.test(line));
  assert.equal(writes.length, 2, writes.join('\n'));
  assert.match(writes[0], /await this\.transport\.write\(frame\)/);
  assert.match(writes[1], /await this\.transport\.write\(word\(received\)\)/);
  assert.match(source, /if \(!ALLOWED\.has\(opcode\)\) throw/);
});

test('the authorization guard runs immediately before every cumulative ACK', async () => {
  const loader = fake();
  const plan = [{op: 'read', address: 0, size: 12288, packet_size: 4096}];
  // After READ_FLASH is written the guard is evaluated twice per packet: before waiting for it, then before its ACK.
  // Expiring on the 4th evaluation = right before the SECOND ACK: only the first ACK may ever reach the port.
  let evaluations = 0;
  const check = () => { if (loader.writes.some(w => w[1] === 0xd2) && ++evaluations === 4) throw new Error('reservation expired'); };
  await assert.rejects(runP4FlasherRead({loader, args: await argsFor(plan), device, stubBytes: bytes, romOnly: true, check}), /expired/);
  assert.equal(loader.writes.filter(w => w.length === 4).length, 1);
});

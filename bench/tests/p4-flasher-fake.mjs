// Shared fixtures (not a *.test.mjs): a simulated read-only flasher stub speaking the READ_FLASH / SPI_FLASH_MD5 subset.
import {createHash} from 'node:crypto';
import {expectedFlasherConsent} from '../web/p4-flasher-read.js';
import {sha256} from '../web/protocol.js';

export const stubText = JSON.stringify({entry: 0x4ff50010, text: Buffer.alloc(100, 0x13).toString('base64'), text_start: 0x4ff50000,
  data: Buffer.alloc(40, 7).toString('base64'), data_start: 0x4ff60040, bss_start: 0x4ff60000});
export const bytes = new TextEncoder().encode(stubText);
export const sha = await sha256(bytes);
export const device = {id: 'p4', chip: 'esp32p4'};
export const NOR = Uint8Array.from({length: 1 << 16}, (_, i) => (i * 31 + (i >> 8)) & 255);   // synthetic content, not a flash dump
export const md5 = data => createHash('md5').update(data).digest();
export const read = value => Array.from(value);

export async function argsFor(plan) {
  return {chip: 'esp32p4', stub_artifact: 'artifact', stub_sha256: sha, scenario: 'read_only_v1', plan,
    consent: await expectedFlasherConsent('p4', sha, plan)};
}

// Loader + simulated stub speaking the read-only subset.
export function fake({badDigest = false, shortPacket = false, failStatus = false} = {}) {
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


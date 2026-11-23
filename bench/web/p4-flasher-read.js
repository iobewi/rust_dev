// Read-only use of the Rust ESP32-P4 flasher stub, loaded in RAM through IDLab's ROM-only session.
// Only three esptool commands can ever be built here: SPI_ATTACH, READ_FLASH and SPI_FLASH_MD5.
// There is no generic "send opcode" entry point: write, erase, register and parameter commands do not exist in this module.
import {HardwareError} from './hardware-errors.js';
import {sha256} from './protocol.js';
import {md5hex} from './md5.js';
import {loadDiagnosticStub} from './p4-stub-loader.js';

export const P4_FLASHER_READ = 'diagnostic.p4_flasher_read';
export const P4_FLASHER_SCENARIO = 'read_only_v1';
export const CAPACITY = 1 << 25;
export const PACKETS = [1024, 2048, 4096];
export const MAX_STEPS = 32, MAX_READ = 4 * 1024 * 1024, MAX_TOTAL = 16 * 1024 * 1024;
// The complete opcode allowlist. Everything else is refused BEFORE a byte is written.
export const OPCODES = Object.freeze({SPI_ATTACH: 0x0d, SPI_FLASH_MD5: 0x13, READ_FLASH: 0xd2});
const ALLOWED = new Set(Object.values(OPCODES));
// BenchTransport.read(timeout) bounds SILENCE, restarted at every received chunk. A response timeout therefore has to cover the
// stub's computation: SPI_FLASH_MD5 stays silent while it reads the NOR (budget: at least 40 KiB/s of NOR reading).
const TIMEOUT_MS = 5000, PACKET_TIMEOUT_MS = 10000;
export const md5Timeout = size => TIMEOUT_MS + Math.ceil(size / 4096) * 100;

const refuse = (code = 'diagnostic_parameter_not_allowed') => new HardwareError(code);
const isInt = value => Number.isInteger(value) && value >= 0;

export function planCanonical(plan) {
  return plan.map(s => `${s.op},${s.address ?? 0},${s.size ?? 0},${s.packet_size ?? 0}`).join(';');
}
export async function expectedFlasherConsent(device, sha, plan) {
  return {scenario: P4_FLASHER_READ, device, stub_sha256: sha,
    plan_sha256: await sha256(new TextEncoder().encode(planCanonical(plan))),
    ram_code_execution: true, flash_write_forbidden: true, rom_reset_may_be_required: true};
}

export function validatePlan(plan) {
  if (!Array.isArray(plan) || plan.length < 1 || plan.length > MAX_STEPS) throw refuse();
  let total = 0;
  plan.forEach((step, index) => {
    if (!step || typeof step !== 'object' || Array.isArray(step)) throw refuse();
    const keys = Object.keys(step).sort().join(',');
    if (step.op === 'attach') {
      if (keys !== 'op' || index !== 0) throw refuse();
      return;
    }
    if (step.op !== 'read' && step.op !== 'md5') throw refuse();
    if (keys !== (step.op === 'read' ? 'address,op,packet_size,size' : 'address,op,size')) throw refuse();
    if (!isInt(step.address) || !isInt(step.size) || step.address >= CAPACITY || step.size < 1 ||
        step.size > (step.op === 'read' ? MAX_READ : CAPACITY) || step.address + step.size > CAPACITY) throw refuse();
    if (step.op === 'read') {
      if (!PACKETS.includes(step.packet_size)) throw refuse();
      total += step.size;
    }
  });
  if (total > MAX_TOTAL) throw refuse();
}

export async function validateFlasherArgs(args, device) {
  const keys = ['chip', 'stub_artifact', 'stub_sha256', 'scenario', 'plan', 'consent'];
  if (!args || typeof args !== 'object' || Array.isArray(args) ||
      Object.keys(args).length !== keys.length || keys.some(k => !(k in args))) throw refuse();
  if (args.chip !== 'esp32p4' || args.scenario !== P4_FLASHER_SCENARIO || device?.chip !== 'esp32p4') throw refuse();
  if (typeof args.stub_artifact !== 'string' || typeof args.stub_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(args.stub_sha256))
    throw refuse('diagnostic_stub_sha256_invalid');
  validatePlan(args.plan);
  const want = await expectedFlasherConsent(device.id, args.stub_sha256, args.plan), got = args.consent;
  if (!got || typeof got !== 'object' || Object.keys(got).length !== Object.keys(want).length ||
      Object.keys(want).some(k => got[k] !== want[k])) throw refuse('diagnostic_consent_invalid');
}

const word = value => Uint8Array.of(value & 255, (value >>> 8) & 255, (value >>> 16) & 255, (value >>> 24) & 255);
const u32 = (bytes, at) => (bytes[at] | bytes[at + 1] << 8 | bytes[at + 2] << 16 | bytes[at + 3] << 24) >>> 0;
const concat = (...parts) => { const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0)); let at = 0; for (const p of parts) { out.set(p, at); at += p.length; } return out; };
const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');

/** The only object able to talk to the running stub. */
export class ReadOnlyFlasher {
  constructor(transport, now = () => performance.now()) { this.transport = transport; this.now = now; this.sent = new Set(); this.commands = 0; }
  async send(opcode, payload) {
    if (!ALLOWED.has(opcode)) throw refuse('diagnostic_flasher_opcode_not_allowed');   // before any write
    const frame = concat(Uint8Array.of(0, opcode), Uint8Array.of(payload.length & 255, payload.length >> 8), word(0), payload);
    this.sent.add(opcode); this.commands++;
    await this.transport.write(frame);
  }
  async response(opcode, dataLength, timeout = TIMEOUT_MS) {
    const frame = await this.transport.read(timeout);
    if (!(frame instanceof Uint8Array) || frame.length < 10 || frame[0] !== 1 || frame[1] !== opcode)
      throw new HardwareError('diagnostic_flasher_bad_response');
    const length = frame[2] | frame[3] << 8;
    if (frame.length !== 8 + length || length !== dataLength + 2) throw new HardwareError('diagnostic_flasher_bad_response');
    if (frame[frame.length - 2] !== 0) throw new HardwareError('diagnostic_flasher_status_error');
    return frame.subarray(8, 8 + dataLength);
  }
  async attach() { await this.send(OPCODES.SPI_ATTACH, word(0)); await this.response(OPCODES.SPI_ATTACH, 0); }
  async md5(address, size) {
    await this.send(OPCODES.SPI_FLASH_MD5, concat(word(address), word(size), word(0), word(0)));
    return hex(await this.response(OPCODES.SPI_FLASH_MD5, 16, md5Timeout(size)));
  }
  async read(address, size, packetSize, check = () => {}) {
    await this.send(OPCODES.READ_FLASH, concat(word(address), word(size), word(packetSize), word(1)));
    await this.response(OPCODES.READ_FLASH, 0);
    const data = new Uint8Array(size);
    let received = 0, packets = 0;
    const start = this.now();
    while (received < size) {
      check();
      const packet = await this.transport.read(PACKET_TIMEOUT_MS);
      // A packet is never longer than the requested size, never empty and never above the negotiated packet size.
      if (!(packet instanceof Uint8Array) || packet.length === 0 || packet.length > packetSize ||
          packet.length !== Math.min(packetSize, size - received)) throw new HardwareError('diagnostic_flasher_bad_packet');
      data.set(packet, received); received += packet.length; packets++;
      // The acknowledgement authorizes the stub to read and send the NEXT packet: the reservation/grant guard is evaluated
      // immediately before it, not only before the wait for the packet.
      check();
      this.commands++;   // the cumulative acknowledgement
      await this.transport.write(word(received));
    }
    const elapsed = this.now() - start;
    const digest = await this.transport.read(TIMEOUT_MS);
    if (!(digest instanceof Uint8Array) || digest.length !== 16) throw new HardwareError('diagnostic_flasher_bad_digest');
    return {data, packets, elapsed_ms: elapsed, md5_stub: hex(digest)};
  }
}

export async function runP4FlasherRead({loader, args, device, stubBytes, romOnly = false, check = () => {}, report = () => {}, now = () => performance.now()}) {
  let stubText;
  try {
    await validateFlasherArgs(args, device);
    if (await sha256(stubBytes) !== args.stub_sha256) throw new HardwareError('artifact_integrity_failed');
    stubText = new TextDecoder('utf-8', {fatal: true}).decode(stubBytes);
    check();
  } catch (error) {
    error.indeterminate = false;   // local refusal: nothing reached the chip
    error.diagnostic = {category: 'local_validation', ram_load: 'not_started', phase: 'diagnostic_stub_load'};
    throw error;
  }
  let started = false, loaded;
  try {
    check(); report({phase: 'ram_load'});
    started = true;
    loaded = await loadDiagnosticStub(loader, stubText, {
      consent: true, expectedSha256: args.stub_sha256, romOnly, protocol: 'flasher', check,
    });
    const flasher = new ReadOnlyFlasher(loader.transport, now);
    const steps = [];
    let index = 0;
    for (const step of args.plan) {
      check(); report({phase: 'diagnostic_read', step: index});
      if (step.op === 'attach') {
        await flasher.attach(); steps.push({op: 'attach', ok: true});
      } else if (step.op === 'md5') {
        steps.push({op: 'md5', address: step.address, size: step.size, md5_stub: await flasher.md5(step.address, step.size)});
      } else {
        const read = await flasher.read(step.address, step.size, step.packet_size, check);
        const md5Host = md5hex(read.data);
        steps.push({op: 'read', address: step.address, size: step.size, packet_size: step.packet_size,
          bytes: read.data.length, packets: read.packets, duration_ms: Math.round(read.elapsed_ms),
          throughput_kib_s: read.elapsed_ms > 0 ? read.data.length / 1024 / (read.elapsed_ms / 1000) : null,
          data_sha256: await sha256(read.data), md5_host: md5Host, md5_stub: read.md5_stub, md5_match: md5Host === read.md5_stub});
        // Stop on the first mismatch: no automatic continuation.
        if (md5Host !== read.md5_stub) throw new HardwareError('diagnostic_flasher_md5_mismatch');
      }
      index++;
    }
    return {scenario: P4_FLASHER_READ, stub_sha256: args.stub_sha256, chip_revision: loaded.chip_revision,
      ram_load: loaded.ram_load, greeting: 'OHAI', steps,
      opcodes_sent: [...flasher.sent].sort((a, b) => a - b), commands_sent: flasher.commands,
      stub_resident: true, requires_operator_reset: true, nor_written: false};
  } catch (error) {
    error.indeterminate = started && error.sessionIndeterminate !== false;
    const admission = error?.sessionState && typeof error.sessionState === 'object' ? error.sessionState : {};
    error.diagnostic = {
      ...admission,
      phase: loaded ? 'diagnostic_read' : 'diagnostic_stub_load',
      category: !error.indeterminate ? 'refused_before_load' : loaded ? 'stream_failed' : 'ram_load_failed',
      ram_load: loaded || error.indeterminate ? 'started' : 'not_started',
    };
    throw error;
  }
}

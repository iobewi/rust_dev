// IDLab P4 only: RAM-only loader of the experimental diagnostic stub.
//
// The standard stub stays the default. This module is never reached from the normal read path:
// it must be called explicitly, with the operator's consent and the SHA-256 of the exact stub JSON
// that was reviewed. It only uses the ROM RAM-download commands (MEM_BEGIN / MEM_DATA / MEM_END):
// it never writes, erases or configures the NOR flash, never resets the chip, never retries.
//
// Every refusal below happens BEFORE the first byte is sent to the chip (the admission only exchanges sync frames).
import {observeSessionState, judgeSessionState, sessionStateReport} from './p4-session-state.js';
import {decodeTraceReport, IDLAB_GET_FLASH_TRACE} from './p4-stub-trace.js';

export const DIAGNOSTIC_STUB_CHIP = 'ESP32-P4';
// Block size of the RAM download over USB-OTG. References: esptool-js 0.7.0 targets/esp32p4.js (USB_RAM_BLOCK = 0x800, applied by
// postConnect() when usesUsbOtg()) and esptool 5.6.0 loader.py (USB_OTG_BLOCK_SIZE = 0x800, applied by _post_connect()): the USB-OTG
// ROM cannot take the default 0x1800 blocks. A ROM-only session never runs main()/postConnect(), so it is applied here, alone.
export const P4_USB_OTG_RAM_BLOCK = 0x800;
// esptool-js 0.7.0: getChipRevision() = major*100 + minor; the stub JSON `esp32p4.json` (the ROM
// that this diagnostic is built for, "ECO5 and later") is selected for revisions >= 3.00.
export const MIN_CHIP_REVISION = 300;
// src/ld/esp32p4.ld of esp-flasher-stub v1.2.2
export const RAM_WINDOWS = Object.freeze({
  text: Object.freeze({start: 0x4ff50000, length: 0x4500}),
  data: Object.freeze({start: 0x4ff60000, length: 0x18000}),
});
const JSON_KEYS = ['entry', 'text', 'text_start', 'data', 'data_start', 'bss_start'];
const FLASH_METHODS = ['flashBegin', 'flashBlock', 'flashFinish', 'flashDeflBegin', 'flashDeflBlock',
  'flashDeflFinish', 'writeFlash', 'eraseFlash', 'eraseRegion', 'flashSpiAttach', 'flashSetParameters'];

export class IdlabDiagnosticError extends Error {
  constructor(code, {chipTouched = false, detail, sessionState, ramLoad} = {}) {
    super(code);
    this.ramLoad = ramLoad;             // typed per-step report of the RAM download (see ramLoadReport)
    this.sessionState = sessionState;   // allowlisted raw observations of the admission (see hardware-errors.js)
    this.name = 'IdlabDiagnosticError';
    this.code = code;
    this.detail = detail;
    // True once any byte reached the chip: the session may be in an undetermined state and must
    // be closed by the operator. Nothing here retries or reconnects by itself.
    this.sessionIndeterminate = chipTouched;
    this.noRetry = true;
  }
}

export async function sha256Hex(bytes) {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

function decodeBase64(text) {
  const raw = globalThis.atob(text);
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

// Pure validation of the loader JSON. Throws IdlabDiagnosticError (chip untouched).
export function parseStubJson(text) {
  let json;
  try { json = JSON.parse(text); } catch { throw new IdlabDiagnosticError('stub_json_not_json'); }
  if (json === null || typeof json !== 'object' || Array.isArray(json) ||
      Object.keys(json).sort().join() !== [...JSON_KEYS].sort().join())
    throw new IdlabDiagnosticError('stub_json_unexpected_keys');
  for (const key of ['entry', 'text_start', 'data_start', 'bss_start'])
    if (!Number.isSafeInteger(json[key]) || json[key] < 0) throw new IdlabDiagnosticError('stub_json_bad_address', {detail: key});
  let decodedText, decodedData;
  try { decodedText = decodeBase64(json.text); decodedData = decodeBase64(json.data); }
  catch { throw new IdlabDiagnosticError('stub_json_bad_base64'); }
  const inside = (window, start, length) => start >= window.start && start + length <= window.start + window.length;
  if (decodedText.length === 0 || !inside(RAM_WINDOWS.text, json.text_start, decodedText.length))
    throw new IdlabDiagnosticError('stub_text_outside_iram', {detail: json.text_start});
  if (decodedData.length === 0 || !inside(RAM_WINDOWS.data, json.data_start, decodedData.length))
    throw new IdlabDiagnosticError('stub_data_outside_dram', {detail: json.data_start});
  if (!(json.bss_start >= RAM_WINDOWS.data.start && json.bss_start < RAM_WINDOWS.data.start + RAM_WINDOWS.data.length))
    throw new IdlabDiagnosticError('stub_bss_outside_dram', {detail: json.bss_start});
  if (!(json.entry >= json.text_start && json.entry < json.text_start + decodedText.length))
    throw new IdlabDiagnosticError('stub_entry_outside_text', {detail: json.entry});
  return {json, decodedText, decodedData};
}

// Fresh, traced ROM/stub admission: never trusts the connection-time flags. The refusal carries the raw observations
// (allowlisted fields only, see hardware-errors.js). It transmits ESP_SYNC frames to the chip (no RAM/flash write, no reset)
// and a refusal never retries.
async function admitRomSession(loader, options) {
  let evidence;
  try { evidence = await observeSessionState(loader, { md5Probe: options.md5Probe }); }
  catch (error) {
    throw new IdlabDiagnosticError('diagnostic_admission_sync_failed', {detail: String(error?.message ?? error).slice(0, 120),
      sessionState: {is_stub: loader.IS_STUB === true ? 1 : 0}});
  }
  const judged = judgeSessionState(evidence);
  const sessionState = { ...sessionStateReport(evidence, judged), admission_verdict: judged.verdict };
  if (judged.verdict === 'rom') return sessionState;   // published with the operation even when the admission succeeds
  throw new IdlabDiagnosticError(judged.verdict === 'stub' ? 'diagnostic_stub_reported_running' : 'diagnostic_session_state_indeterminate',
    {sessionState});
}

// Per-step trace of the RAM download (MEM_BEGIN / MEM_DATA / MEM_END): it only OBSERVES, the commands, their arguments, their
// order and the loader's own timeouts are untouched. Each entry distinguishes a command that was started from one the chip
// acknowledged. Only typed, bounded fields are kept: never the stub bytes, never loader text.
const OUTCOMES = new Set(['ack', 'timeout', 'closed', 'invalid', 'error']);
export function classifyRamLoadError(error) {
  if (error?.kind === 'no-response' || /^(No serial data received|Serial data stream stopped)/.test(String(error?.message ?? ''))) return 'timeout';
  if (error?.kind === 'closed' || error?.name === 'NetworkError') return 'closed';
  if (error?.kind === 'partial-packet' || error?.kind === 'invalid-packet') return 'invalid';
  return 'error';
}
// The published list is bounded (hardware-errors.js accepts at most 32 entries, all or nothing). Beyond that, the first entries are kept
// together with the LAST TWO (the last acknowledged command and the failing one, or the final two commands on success), and
// `steps_omitted` counts the dropped middle entries. `failed_index` / `acked_count` keep the original numbering. The qualified
// stub (text 8952 B + data 276 B) needs 9 commands with 0x800 blocks (6 with 0x1800): the bound is never reached by it.
export const MAX_REPORTED_RAM_STEPS = 32;
function boundedSteps(entries) {
  const copy = entries.map(e => ({...e}));
  if (copy.length <= MAX_REPORTED_RAM_STEPS) return {steps: copy, omitted: 0};
  return {steps: [...copy.slice(0, MAX_REPORTED_RAM_STEPS - 2), ...copy.slice(-2)], omitted: copy.length - MAX_REPORTED_RAM_STEPS};
}
function ramLoadReport(ramBlock, adaptation = {}) {
  const entries = [];
  const view = () => {
    const acked = entries.filter(e => e.acked === 1).length;
    const failed = entries.findIndex(e => e.acked === 0);
    const {steps, omitted} = boundedSteps(entries);
    return {ram_block: ramBlock, ...adaptation, steps, steps_omitted: omitted, acked_count: acked, failed_index: failed >= 0 ? failed : null,
      last_acked_index: acked > 0 && entries.slice(0, acked).every(e => e.acked === 1) ? acked - 1 : null};
  };
  return {
    snapshot: view,
    async run(meta, now, command) {
      const entry = {...meta, ram_block: ramBlock, started: 1, acked: 0, duration_ms: 0, outcome: 'error'};
      entries.push(entry);                              // recorded BEFORE the command: started, not yet acknowledged
      const t0 = now();
      try { await command(); entry.acked = 1; entry.outcome = 'ack'; }
      catch (error) { entry.outcome = classifyRamLoadError(error); throw error; }
      finally { entry.duration_ms = Math.max(0, Math.round(now() - t0)); }
    },
  };
}
export const RAM_LOAD_OUTCOMES = OUTCOMES;

// Applies the official USB-OTG RAM block size, and nothing else of postConnect() (no watchdog change, no flash power-on, no
// register write, no reset). The transport must be POSITIVELY identified as USB-OTG: anything else is refused, never assumed.
async function adaptRamBlock(loader, admission) {
  const defaultBlock = loader.ESP_RAM_BLOCK;
  let otg;
  try { otg = typeof loader.usesUsbOtg === 'function' ? await loader.usesUsbOtg() : undefined; }
  catch (error) {
    if (['closed', 'no-response', 'partial-packet', 'invalid-packet'].includes(error?.kind) || error?.name === 'NetworkError') throw error;   // a lost transport keeps its own code
    otg = undefined;
  }
  if (otg !== true) throw new IdlabDiagnosticError('diagnostic_transport_unconfirmed', {sessionState: admission});
  // The library's own USB-OTG value must be present AND equal to the reviewed one: an absent, mistyped or different value is refused,
  // never defaulted (esptool-js 0.7.0 esp32p4.js declares USB_RAM_BLOCK = 0x800).
  if (loader.chip?.USB_RAM_BLOCK !== P4_USB_OTG_RAM_BLOCK)
    throw new IdlabDiagnosticError('diagnostic_ram_block_unexpected', {sessionState: admission});
  loader.ESP_RAM_BLOCK = P4_USB_OTG_RAM_BLOCK;
  return {ram_block_default: defaultBlock, ram_block_source: 'usb_otg_cap', usb_otg_confirmed: 1};
}

// Loads the reviewed diagnostic stub into RAM of a ROM-mode ESP32-P4 and positively identifies it.
//   options.consent         must be exactly true (operator's explicit consent for THIS binary)
//   options.expectedSha256  SHA-256 (hex) of the stub JSON text that was reviewed and qualified
export async function loadDiagnosticStub(loader, stubJsonText, options = {}) {
  if (options.consent !== true) throw new IdlabDiagnosticError('diagnostic_stub_consent_missing');
  if (!/^[0-9a-f]{64}$/.test(options.expectedSha256 ?? '')) throw new IdlabDiagnosticError('diagnostic_stub_sha256_missing');
  if (options.romOnly !== true) throw new IdlabDiagnosticError('diagnostic_rom_only_required');   // the adaptation below exists for the ROM-only session only
  if (loader?.__idlabRamLoadStarted === true) throw new IdlabDiagnosticError('diagnostic_ram_load_already_started');
  const actual = await sha256Hex(new TextEncoder().encode(stubJsonText));
  if (actual !== options.expectedSha256) throw new IdlabDiagnosticError('diagnostic_stub_sha256_mismatch', {detail: actual});
  if (loader?.chip?.CHIP_NAME !== DIAGNOSTIC_STUB_CHIP) throw new IdlabDiagnosticError('diagnostic_stub_wrong_chip');
  const admission = await admitRomSession(loader, options);
  const revision = await loader.chip.getChipRevision(loader);  // read-only eFuse reads
  if (!Number.isInteger(revision) || revision < MIN_CHIP_REVISION)
    throw new IdlabDiagnosticError('diagnostic_stub_unsupported_chip_revision', {detail: revision});
  const {json, decodedText, decodedData} = parseStubJson(stubJsonText);

  const adaptation = await adaptRamBlock(loader, admission);   // before the first memBegin; a refusal here leaves the RAM untouched

  // ---- from here on the chip is touched: any failure leaves the session undetermined ----
  loader.__idlabRamLoadStarted = true;
  const now = options.now ?? (() => performance.now());
  const steps = ramLoadReport(loader.ESP_RAM_BLOCK, adaptation);
  const touched = (code, detail) => new IdlabDiagnosticError(code, {chipTouched: true, detail, sessionState: admission, ramLoad: steps.snapshot()});
  try {
    for (const [segment, bytes, start] of [['text', decodedText, json.text_start], ['data', decodedData, json.data_start]]) {
      const block = loader.ESP_RAM_BLOCK;
      const blocks = Math.floor((bytes.length + block - 1) / block);
      await steps.run({step: 'mem_begin', segment, blocks, address: start, size: bytes.length}, now,
        () => loader.memBegin(bytes.length, blocks, block, start));
      for (let seq = 0; seq < blocks; seq++) {
        const chunk = bytes.slice(seq * block, (seq + 1) * block);
        await steps.run({step: 'mem_block', segment, block: seq, blocks, address: start + seq * block, size: chunk.length}, now,
          () => loader.memBlock(chunk, seq));
      }
    }
    await steps.run({step: 'mem_finish', segment: 'entry', address: json.entry, size: 0}, now, () => loader.memFinish(json.entry));
  } catch (error) {
    throw touched('diagnostic_stub_ram_load_failed', String(error?.message ?? error));
  }
  let greeting;
  try { greeting = String.fromCharCode(...await loader.transport.read(loader.DEFAULT_TIMEOUT)); }
  catch (error) { throw touched('diagnostic_stub_no_greeting', String(error?.message ?? error)); }
  if (greeting !== 'OHAI') throw touched('diagnostic_stub_bad_greeting', greeting);
  loader.IS_STUB = true;
  loader.__idlabTraceStub = false;  // not yet identified
  try {
    // A stock stub rejects the diagnostic opcode: the flag is only set after a valid IDT1 answer.
    decodeTraceReport(await loader.checkCommand('IDLab trace probe', IDLAB_GET_FLASH_TRACE,
      new Uint8Array(0), undefined, 64, 3000));
  } catch (error) {
    throw touched('diagnostic_stub_not_identified', String(error?.message ?? error));
  }
  loader.__idlabTraceStub = true;
  loader.__idlabTraceStubSha256 = actual;
  return {sha256: actual, entry: json.entry, chip_revision: revision, text_bytes: decodedText.length,
    data_bytes: decodedData.length, ram_only: true, admission, ram_load: steps.snapshot()};
}

// Explicit end of the diagnostic mode, on the browser side only. The stub stays in the chip RAM until
// the operator-approved reset of the standard IDLab procedure; this function never resets anything.
export function retireDiagnosticStub(loader) {
  loader.__idlabTraceStub = false;
  loader.__idlabTraceStubSha256 = undefined;
  loader.IS_STUB = false;
  return {requires_operator_reset: true};
}

export const FORBIDDEN_FLASH_METHODS = FLASH_METHODS;

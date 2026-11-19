// Experimental ESP32-P4 USB OTG diagnostic: orchestration only (no DOM, no REST).
// Everything here is re-validated locally: the server and the UI are not trusted to have checked.
import { HardwareError } from './hardware-errors.js';
import { sha256 } from './protocol.js';
import { loadDiagnosticStub, retireDiagnosticStub, IdlabDiagnosticError } from './p4-stub-loader.js';
import { readFlashWithP4StubTrace, convertTraceCycles, impliedCpuHz, IdlabTraceError } from './p4-stub-trace.js';

export const P4_TRACE = 'diagnostic.p4_usb_trace';
export const P4_FIXED = Object.freeze({ chip: 'esp32p4', address: 0x8000, size: 1048576, packet_size: 4096, cpu_hz: 200000000 });
const CONSENT_KEYS = ['scenario', 'device', 'stub_sha256', 'address', 'size', 'ram_code_execution',
  'session_effects_acknowledged', 'rom_reset_may_be_required'];

export function expectedConsent(device, stubSha256) {
  return { scenario: P4_TRACE, device, stub_sha256: stubSha256, address: P4_FIXED.address, size: P4_FIXED.size,
    ram_code_execution: true, session_effects_acknowledged: true, rom_reset_may_be_required: true };
}
const fail = (code, detail) => new HardwareError(code, detail);

/** Strict local check of the request. Throws before anything touches the port. */
export function validateDiagnosticArgs(args, device) {
  if (!args || typeof args !== 'object') throw fail('diagnostic_parameter_not_allowed');
  const keys = new Set(['chip', 'stub_artifact', 'stub_sha256', 'address', 'size', 'packet_size', 'cpu_hz', 'consent']);
  if (Object.keys(args).some(k => !keys.has(k)) || [...keys].some(k => !(k in args))) throw fail('diagnostic_parameter_not_allowed');
  for (const [key, value] of Object.entries(P4_FIXED)) if (args[key] !== value) throw fail('diagnostic_parameter_not_allowed', { field: key });
  if (device?.chip !== P4_FIXED.chip) throw fail('diagnostic_chip_unsupported');
  if (typeof args.stub_artifact !== 'string' || !/^[0-9a-f]{64}$/.test(args.stub_sha256)) throw fail('diagnostic_stub_sha256_invalid');
  const want = expectedConsent(device.id, args.stub_sha256), got = args.consent;
  if (!got || typeof got !== 'object' || Object.keys(got).length !== CONSENT_KEYS.length ||
      CONSENT_KEYS.some(k => got[k] !== want[k])) throw fail('diagnostic_consent_invalid');
}

/**
 * Runs the whole diagnostic on a loader that is in a ROM session (no stub loaded yet).
 * Never retries, never resets, never falls back to the standard stub.
 * Returns { result }. On a failure after the chip was touched it throws an error with `chipTouched = true`.
 */
export async function runP4UsbTrace({ loader, args, device, stubBytes, check = () => {}, report = () => {}, now = () => performance.now() }) {
  // Structured, allowlisted origin report attached to every failure (see hardware-errors.js). Never carries loader text.
  const report_ = (error, fields) => { error.diagnostic = { origin_code: error.code, origin_error_type: error.name, ...fields }; return error; };
  let stubText;
  try {   // refusals here happen before anything is sent to the chip
    validateDiagnosticArgs(args, device);
    if (await sha256(stubBytes) !== args.stub_sha256) throw fail('artifact_integrity_failed');
    stubText = new TextDecoder('utf-8', { fatal: true }).decode(stubBytes);
    check();
  } catch (error) { error.indeterminate = false; throw report_(error, { phase: 'diagnostic_stub_load', category: 'local_validation', ram_load: 'not_started' }); }
  let touched = false;
  const wasStub = loader.IS_STUB === true;   // a standard stub already running is refused by the loader, not 'launched' by us
  try {
    report({ phase: 'diagnostic_stub_load' });
    // loadDiagnosticStub refuses (chip untouched) unless: consent, sha, ESP32-P4, rev >= 3.00, ROM session.
    touched = true; // conservative: from here a failure may have reached the chip, unless the loader says otherwise
    const loaded = await loadDiagnosticStub(loader, stubText, { consent: true, expectedSha256: args.stub_sha256 });
    check();
    report({ phase: 'diagnostic_read' });
    const start = now();
    let packets = 0;
    const read = await readFlashWithP4StubTrace(loader, args.address, args.size, () => { packets += 1; if (packets % 16 === 0) { report({ phase: 'diagnostic_read', packets }); check(); } });
    const wallMs = Math.round(now() - start);
    const converted = convertTraceCycles(read.trace, args.cpu_hz);
    return { result: {
      scenario: P4_TRACE, stub_sha256: args.stub_sha256, chip_revision: loaded.chip_revision,
      address: args.address, size: args.size, packet_size: args.packet_size,
      data_sha256: await sha256(read.data), md5: read.md5, host_duration_ms: wallMs,
      trace: read.trace, converted,
      cpu_hz: { value: args.cpu_hz, basis: 'assumed_from_stub_code_not_measured', implied_by_wall_time: impliedCpuHz(read.trace, wallMs) },
      packets: read.trace.packets, acks: read.trace.ack_count, bytes: read.trace.bytes,
      rom_cdc_fill_calls: read.trace.fill_calls,
      stub_resident: true, requires_operator_reset: true, nor_written: false } };
  } catch (error) {
    const known = error instanceof IdlabDiagnosticError || error instanceof IdlabTraceError;
    // Indeterminate as soon as anything may have reached the chip: unknown failures, a loader that says so,
    // or a stub already executing (IS_STUB set by the loader after the RAM launch).
    const launched = loader.IS_STUB === true && !wasStub;
    const indeterminate = !known || error.sessionIndeterminate === true || launched;
    error.indeterminate = touched && indeterminate;
    const stubLoaded = launched || error instanceof IdlabTraceError;
    const fields = { phase: stubLoaded ? 'diagnostic_read' : 'diagnostic_stub_load' };
    if (error instanceof IdlabTraceError) Object.assign(fields, { category: 'stream_failed', ram_load: 'started' });
    else if (error instanceof IdlabDiagnosticError && error.sessionIndeterminate !== true && !launched)
      Object.assign(fields, { category: 'refused_before_load', ram_load: 'not_started' });   // the loader's pre-write checks: nothing reached the chip
    else if (error instanceof IdlabDiagnosticError) Object.assign(fields, { category: 'ram_load_failed', ram_load: 'started' });
    else Object.assign(fields, { category: 'unexpected', ram_load: stubLoaded ? 'started' : 'unknown' });
    if (error instanceof IdlabDiagnosticError && Number.isInteger(error.detail) && /revision/.test(error.code)) fields.chip_revision = error.detail;
    throw report_(error, fields);
  }
}
export { retireDiagnosticStub };

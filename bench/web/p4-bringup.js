// Minimal ESP32-P4 CDC bring-up, using IDLab's existing ROM-only Transport.
// Only OHAI and two SLIP PING/PONG exchanges; never any flash commands.
import {HardwareError} from './hardware-errors.js';
import {sha256} from './protocol.js';
import {loadDiagnosticStub} from './p4-stub-loader.js';

export const P4_BRINGUP = 'diagnostic.p4_usb_bringup';
export const P4_BRINGUP_SCENARIO = 'ohai_ping2';
const REQUEST = new Uint8Array([80, 73, 78, 71]); // PING
const RESPONSE = new Uint8Array([80, 79, 78, 71]); // PONG

export function expectedBringupConsent(device, sha) {
  return {scenario: P4_BRINGUP, device, stub_sha256: sha,
    ram_code_execution: true, rom_reset_may_be_required: true};
}

export function validateBringupArgs(args, device) {
  const keys = ['chip', 'stub_artifact', 'stub_sha256', 'scenario', 'consent'];
  if (!args || typeof args !== 'object' || Array.isArray(args) ||
      Object.keys(args).length !== keys.length || keys.some(k => !(k in args)))
    throw new HardwareError('diagnostic_parameter_not_allowed');
  if (args.chip !== 'esp32p4' || args.scenario !== P4_BRINGUP_SCENARIO || device?.chip !== 'esp32p4')
    throw new HardwareError('diagnostic_parameter_not_allowed');
  if (typeof args.stub_artifact !== 'string' || !/^[0-9a-f]{64}$/.test(args.stub_sha256))
    throw new HardwareError('diagnostic_stub_sha256_invalid');
  const want = expectedBringupConsent(device.id, args.stub_sha256), got = args.consent;
  if (!got || typeof got !== 'object' || Object.keys(got).length !== Object.keys(want).length ||
      Object.keys(want).some(k => got[k] !== want[k]))
    throw new HardwareError('diagnostic_consent_invalid');
}

const equals = (bytes, expected) => bytes instanceof Uint8Array &&
  bytes.length === expected.length && bytes.every((value, index) => value === expected[index]);

export async function runP4UsbBringup({
  loader, args, device, stubBytes, romOnly = false, check = () => {}, report = () => {},
}) {
  let stubText;
  try {
    validateBringupArgs(args, device);
    if (await sha256(stubBytes) !== args.stub_sha256)
      throw new HardwareError('artifact_integrity_failed');
    stubText = new TextDecoder('utf-8', {fatal: true}).decode(stubBytes);
    check();
  } catch (error) {
    // Local refusal: no RAM write.
    error.indeterminate = false;
    error.diagnostic = {category: 'local_validation', ram_load: 'not_started', phase: 'diagnostic_stub_load'};
    throw error;
  }

  let started = false, loaded;
  try {
    check(); report({phase: 'ram_load'});
    started = true;
    loaded = await loadDiagnosticStub(loader, stubText, {
      consent: true, expectedSha256: args.stub_sha256, romOnly, protocol: 'bringup',
    });
    check();
    const exchanges = [];
    for (let index = 1; index <= 2; index++) {
      report({phase: 'ping_pong', exchange: index});
      // BenchTransport adds/removes SLIP. Do not add delimiters here.
      await loader.transport.write(REQUEST);
      const reply = await loader.transport.read(3000);
      if (!equals(reply, RESPONSE)) throw new HardwareError('diagnostic_bringup_unexpected_pong');
      exchanges.push({index, request: 'PING', response: 'PONG', passed: true});
      check();
    }
    return {scenario: P4_BRINGUP, stub_sha256: args.stub_sha256,
      chip_revision: loaded.chip_revision, ram_load: loaded.ram_load,
      greeting: 'OHAI', exchanges, stub_resident: true,
      requires_operator_reset: true, nor_written: false};
  } catch (error) {
    // Loader differentiates safe preflight refusals from an uncertain
    // partial RAM upload. Never retry or automatically release the session.
    error.indeterminate = started && error.sessionIndeterminate !== false;
    error.diagnostic = {
      phase: loaded ? 'diagnostic_read' : 'diagnostic_stub_load',
      category: !error.indeterminate ? 'refused_before_load' : loaded ? 'stream_failed' : 'ram_load_failed',
      ram_load: loaded || error.indeterminate ? 'started' : 'not_started',
    };
    throw error;
  }
}

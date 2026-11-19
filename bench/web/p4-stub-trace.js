// IDLab P4 only: host half of the experimental RAM-loaded stub trace.
// Neither the stock READ_FLASH (0xD2) nor the normal browser loader is touched.
// The diagnostic stub must be explicitly installed and positively identified
// (see p4-stub-loader.js) before this function can be invoked.
export const IDLAB_READ_FLASH_TRACE = 0xdf;
export const IDLAB_GET_FLASH_TRACE = 0xe0;
const REPORT_SIZE = 64;
const MAX_SIZE = 1024 * 1024;
const PACKET_SIZE = 4096;
const TIMEOUT_MS = 120000;
const CYCLE_KEYS = ['flash_cycles', 'md5_cycles', 'send_cycles', 'ack_cycles', 'fill_cycles'];

// Error raised by the trace. `sessionIndeterminate` is true once the bulk stream started: the stub
// may still be waiting for an acknowledgement, so the operator must close the session. Nothing here
// retries, reconnects or resets (`noRetry`).
export class IdlabTraceError extends Error {
  constructor(code, {streamStarted = false, cause} = {}) {
    super(code);
    this.name = 'IdlabTraceError';
    this.code = code;
    this.sessionIndeterminate = streamStarted;
    this.noRetry = true;
    if (cause !== undefined) this.cause = cause;
  }
}

function u32(n) {
  const b = new Uint8Array(4);
  new DataView(b.buffer).setUint32(0, n, true);
  return b;
}

export function decodeTraceReport(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length !== REPORT_SIZE)
    throw new Error('invalid_idlab_trace_length');
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.getUint32(0, true) !== 0x31544449 || view.getUint32(4, true) !== 1)
    throw new Error('invalid_idlab_trace_signature');
  const report = {version: 1};
  let offset = 8;
  for (const key of CYCLE_KEYS) {
    // BigInt values are converted to decimal strings for JSON transport.
    report[key] = view.getBigUint64(offset, true).toString();
    offset += 8;
  }
  for (const key of ['fill_calls', 'packets', 'ack_count', 'bytes']) {
    report[key] = view.getUint32(offset, true);
    offset += 4;
  }
  return report;
}

// Cycles are what the stub counts; microseconds exist only if the caller states the CPU clock.
// `fill_cycles` is part of `send_cycles` (the ROM FIFO fill happens inside send_frame): it is reported
// separately and `send_without_fill` is derived, the two are never to be added.
export function convertTraceCycles(report, cpuHz) {
  if (!Number.isSafeInteger(cpuHz) || cpuHz <= 0) throw new RangeError('cpu_hz_required');
  const us = key => Number(BigInt(report[key]) * 1000000n / BigInt(cpuHz));
  const send = BigInt(report.send_cycles), fill = BigInt(report.fill_cycles);
  if (fill > send) throw new Error('fill_cycles_exceeds_send_cycles');
  return {
    cpu_hz: cpuHz,
    cycles: Object.fromEntries(CYCLE_KEYS.map(k => [k, report[k]])),
    microseconds: {
      flash: us('flash_cycles'), md5: us('md5_cycles'), send_total: us('send_cycles'),
      fill_within_send: us('fill_cycles'), ack_wait: us('ack_cycles'),
      send_without_fill: Number((send - fill) * 1000000n / BigInt(cpuHz)),
    },
    note: 'fill is a subset of send; do not add them',
  };
}

// Counted cycles over the wall time of the traced run. Close to the CPU clock if the counter runs at
// the assumed frequency and the transfer loop dominates the run. A cross-check, never a measurement
// of the clock itself.
export function impliedCpuHz(report, wallMs) {
  if (!(wallMs > 0)) throw new RangeError('wall_time_required');
  const counted = ['flash_cycles', 'md5_cycles', 'send_cycles', 'ack_cycles']
    .reduce((sum, key) => sum + BigInt(report[key]), 0n);
  return Number(counted) / (wallMs / 1000);
}

export async function readFlashWithP4StubTrace(loader, address, size, onPacket = null) {
  if (loader?.IS_STUB !== true || loader?.__idlabTraceStub !== true)
    throw new IdlabTraceError('idlab_diagnostic_stub_not_verified');
  if (!Number.isSafeInteger(address) || address < 0 ||
      !Number.isSafeInteger(size) || size <= 0 || size > MAX_SIZE ||
      address + size > 0x100000000) throw new RangeError('invalid_idlab_trace_range');

  // Verify that the RAM stub really understands our trace opcode before
  // any bulk read; a stock stub must be rejected with no stream started.
  try {
    decodeTraceReport(await loader.checkCommand('IDLab trace probe', IDLAB_GET_FLASH_TRACE,
      new Uint8Array(0), undefined, REPORT_SIZE, 3000));
  } catch (cause) { throw new IdlabTraceError('idlab_trace_probe_failed', {cause}); }

  const payload = new Uint8Array(16);
  const view = new DataView(payload.buffer);
  view.setUint32(0, address, true);
  view.setUint32(4, size, true);
  view.setUint32(8, PACKET_SIZE, true);
  view.setUint32(12, 1, true); // Same single in-flight policy as stock stub.
  const streaming = code => new IdlabTraceError(code, {streamStarted: true});
  let result;
  try { result = await loader.checkCommand('IDLab trace read', IDLAB_READ_FLASH_TRACE, payload); }
  catch (cause) { throw new IdlabTraceError('idlab_trace_command_failed', {streamStarted: true, cause}); }
  if (result !== 0) throw streaming('idlab_trace_command_failed');

  const data = new Uint8Array(size);
  let received = 0, count = 0;
  try {
    while (received < size) {
      const p = await loader.transport.read(TIMEOUT_MS);
      if (!(p instanceof Uint8Array) || p.length === 0 ||
          p.length > PACKET_SIZE || received + p.length > size)
        throw streaming('idlab_trace_invalid_data_frame');
      data.set(p, received);
      received += p.length;
      count++;
      await loader.transport.write(u32(received));
      onPacket?.(p, received, size);
    }
    // Drain the final MD5 frame before sending another command. Otherwise
    // checkCommand() may encounter the trailing frame and desynchronize.
    const md5Bytes = await loader.transport.read(TIMEOUT_MS);
    if (!(md5Bytes instanceof Uint8Array) || md5Bytes.length !== 16)
      throw streaming('idlab_trace_missing_md5_frame');
    const metrics = decodeTraceReport(await loader.checkCommand(
      'IDLab trace counters', IDLAB_GET_FLASH_TRACE, new Uint8Array(0),
      undefined, REPORT_SIZE, 3000));
    if (metrics.bytes !== size || metrics.packets !== count || metrics.ack_count !== count)
      throw streaming('idlab_trace_counter_mismatch');
    return {
      data, trace: metrics,
      md5: [...md5Bytes].map(b => b.toString(16).padStart(2,'0')).join(''),
      cycle_unit: 'cpu_cycles', clock_hz: null,
    };
  } catch (error) {
    if (error instanceof IdlabTraceError) throw error;
    throw new IdlabTraceError('idlab_trace_stream_failed', {streamStarted: true, cause: error});
  }
}

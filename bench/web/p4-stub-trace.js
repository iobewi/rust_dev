// IDLab P4 only: host half of the experimental RAM-loaded stub trace.
// Neither the stock READ_FLASH (0xD2) nor the normal browser loader is touched.
// The diagnostic stub must be explicitly installed and positively identified
// before this function can be invoked.
export const IDLAB_READ_FLASH_TRACE = 0xdf;
export const IDLAB_GET_FLASH_TRACE = 0xe0;
const REPORT_SIZE = 64;
const MAX_SIZE = 1024 * 1024;
const PACKET_SIZE = 4096;
const TIMEOUT_MS = 120000;
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
  for (const key of ['flash_cycles','md5_cycles','send_cycles','ack_cycles','fill_cycles']) {
    // BigInt values are converted to decimal strings for JSON transport.
    report[key] = view.getBigUint64(offset, true).toString();
    offset += 8;
  }
  for (const key of ['fill_calls','packets','ack_count','bytes']) {
    report[key] = view.getUint32(offset, true);
    offset += 4;
  }
  return report;
}
export async function readFlashWithP4StubTrace(loader, address, size, onPacket = null) {
  if (loader?.IS_STUB !== true || loader?.__idlabTraceStub !== true)
    throw new Error('idlab_diagnostic_stub_not_verified');
  if (!Number.isSafeInteger(address) || address < 0 ||
      !Number.isSafeInteger(size) || size <= 0 || size > MAX_SIZE ||
      address + size > 0x100000000) throw new RangeError('invalid_idlab_trace_range');

  const payload = new Uint8Array(16);
  const view = new DataView(payload.buffer);
  view.setUint32(0, address, true);
  view.setUint32(4, size, true);
  view.setUint32(8, PACKET_SIZE, true);
  view.setUint32(12, 1, true); // Same single in-flight policy as stock stub.
  const result = await loader.checkCommand('IDLab trace read', IDLAB_READ_FLASH_TRACE, payload);
  if (result !== 0) throw new Error('idlab_trace_command_failed');

  const data = new Uint8Array(size);
  let received = 0, count = 0;
  while (received < size) {
    const p = await loader.transport.read(TIMEOUT_MS);
    if (!(p instanceof Uint8Array) || p.length === 0 ||
        p.length > PACKET_SIZE || received + p.length > size)
      throw new Error('idlab_trace_invalid_data_frame');
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
    throw new Error('idlab_trace_missing_md5_frame');
  const metrics = decodeTraceReport(await loader.checkCommand(
    'IDLab trace counters', IDLAB_GET_FLASH_TRACE, new Uint8Array(0),
    undefined, REPORT_SIZE, 3000));
  if (metrics.bytes !== size || metrics.packets !== count || metrics.ack_count !== count)
    throw new Error('idlab_trace_counter_mismatch');
  return {
    data, trace: metrics,
    md5: [...md5Bytes].map(b => b.toString(16).padStart(2,'0')).join(''),
    cycle_unit: 'cpu_cycles', clock_hz: null,
  };
}

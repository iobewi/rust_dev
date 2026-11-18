// Connection settings belong to one board and survive engine replacement.
const uartBauds = new Set([115200, 230400, 460800, 921600]);

export function normalizeLoaderBaud(value) {
  if (value === null || value === undefined || value === '') return null;
  if (!Number.isInteger(value) || !uartBauds.has(value)) throw new RangeError('loader_baud_invalid');
  return value;
}

export function effectiveLoaderBaud(native, requested, fallback = 115200) {
  const baud = normalizeLoaderBaud(requested);
  // Native USB carries packets directly, regardless of the nominal CDC baud.
  // For external UART, 115200 is the conservative default: no automatic step-up.
  return native ? 115200 : baud ?? fallback;
}

/** Effective flash throughput, excluding HTTP artifact download/upload overhead. */
export function flashTransfer(bytes, elapsedMs) {
  if (!Number.isSafeInteger(bytes) || bytes < 0 || !Number.isFinite(elapsedMs) || elapsedMs < 0)
    throw new RangeError('flash_transfer_invalid');
  return { bytes, elapsed_ms: Math.round(elapsedMs), bytes_per_second: Math.round(bytes * 1000 / Math.max(elapsedMs, 1)) };
}

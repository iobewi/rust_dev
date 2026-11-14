// Connection settings belong to one board and survive engine replacement.
const uartBauds = new Set([115200, 230400, 460800, 921600]);

export function normalizeLoaderBaud(value) {
  if (value === null || value === undefined || value === '') return null;
  if (!Number.isInteger(value) || !uartBauds.has(value)) throw new RangeError('loader_baud_invalid');
  return value;
}

export function effectiveLoaderBaud(native, requested, fallback = 460800) {
  const baud = normalizeLoaderBaud(requested);
  // Native USB has no UART clock; do not request an esptool baud-rate change.
  return native ? 115200 : baud ?? fallback;
}

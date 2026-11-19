// Read-only ESP_SPI_FLASH_MD5 diagnostic using the stock esptool-js stub.
// The timeout is longer than flashMd5sum()'s default 8 seconds/MiB, because
// ESP32-P4 32MiB flashes may use slow 4-byte addressing.
export const MD5_DIAGNOSTIC_MAX_SIZE = 1024 * 1024;
export const MD5_DIAGNOSTIC_TIMEOUT_MS = 120_000;

export async function flashMd5Diagnostic(loader, address, size, timeout = MD5_DIAGNOSTIC_TIMEOUT_MS) {
  if (!Number.isSafeInteger(address) || address < 0 || address > 0xffffffff ||
      !Number.isSafeInteger(size) || size <= 0 || size > MD5_DIAGNOSTIC_MAX_SIZE ||
      address + size > 0x100000000) throw new RangeError('invalid_flash_md5_region');
  if (!Number.isSafeInteger(timeout) || timeout < 1000 || timeout > 120_000)
    throw new RangeError('invalid_flash_md5_timeout');
  if (loader.IS_STUB !== true) throw new Error('flash_md5_requires_stub');
  const payload = new Uint8Array(16);
  const view = new DataView(payload.buffer);
  view.setUint32(0, address, true);
  view.setUint32(4, size, true);
  const answer = await loader.checkCommand('calculate md5sum', loader.ESP_SPI_FLASH_MD5,
    payload, undefined, 16, timeout);
  if (!(answer instanceof Uint8Array) || answer.byteLength !== 16)
    throw new Error('invalid_flash_md5_response');
  return [...answer].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

// Experimental ESP_READ_FLASH reader for the browser-owned Web Serial session.
// It preserves esptool-js 0.7.0's command, 4096-byte frames, 1024 outstanding
// packet parameter and cumulative 4-byte ACKs; only accumulation is changed:
// one allocation with direct copies instead of repeated full-buffer append.
// No port reopen, reset, retry, extra command or flash modification.
const FRAME_SIZE = 0x1000;
const MAX_IN_FLIGHT = 1024;
function u32(value) {
  const bytes = new Uint8Array(4);
  new DataView(bytes.buffer).setUint32(0, value >>> 0, true);
  return bytes;
}

export async function readFlashPreallocated(loader, address, size, onPacketReceived = null) {
  if (!Number.isSafeInteger(address) || address < 0 || address > 0xffffffff ||
      !Number.isSafeInteger(size) || size <= 0 || size > 0x100000 ||
      address + size > 0x100000000) throw new RangeError('invalid_flash_read_region');
  const command = new Uint8Array(16), view = new DataView(command.buffer);
  view.setUint32(0, address, true);
  view.setUint32(4, size, true);
  view.setUint32(8, FRAME_SIZE, true);
  view.setUint32(12, MAX_IN_FLIGHT, true);

  const status = await loader.checkCommand('read flash', loader.ESP_READ_FLASH, command);
  if (status != 0) throw new Error('flash_read_command_failed');

  const result = new Uint8Array(size);
  let received = 0;
  while (received < size) {
    const packet = await loader.transport.read(loader.FLASH_READ_TIMEOUT);
    if (!(packet instanceof Uint8Array) || packet.length === 0 ||
        packet.length > FRAME_SIZE || packet.length > size - received)
      throw new Error('flash_read_invalid_packet');
    result.set(packet, received);
    received += packet.length;
    // Identical to esptool-js 0.7.0: ACK the cumulative byte count even
    // for the final packet, before invoking the progress callback.
    await loader.transport.write(u32(received));
    if (onPacketReceived) onPacketReceived(packet, received, size);
  }
  return result;
}

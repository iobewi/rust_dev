// ES module served only by Playwright's external-module route. This is not a real ESP loader.
export class ESPError extends Error {}
export class Transport {
  constructor(port) { this.port = port; this.buffer = new Uint8Array(); }
  async connect() { await this.port.open({ baudRate: 115200 }); }
  async disconnect() { await this.port.close(); }
  async readLoop() {}
}
export class ESPLoader {
  constructor({ transport }) { this.transport = transport; this.chip = { readMac: async () => '00:00:00:00:00:00' }; }
  async main() { await this.transport.connect(); return 'ESP32-S3'; }
  async readFlashId() { return 0x164068; }
  async readFlash(address, size) {
    const bytes = new Uint8Array(size).fill(255);
    if (address === 0x8000 && size >= 64) {
      const view = new DataView(bytes.buffer); view.setUint16(0, 0x50aa, true); bytes[2] = 0; bytes[3] = 0;
      view.setUint32(4, 0x10000, true); view.setUint32(8, 0x20000, true);
      bytes.fill(0, 12, 32); bytes.set(new TextEncoder().encode('factory'), 12);
    }
    return bytes;
  }
}

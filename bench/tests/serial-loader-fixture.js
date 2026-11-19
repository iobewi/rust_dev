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
  async checkCommand(_label, _opcode, params) {
    const view = new DataView(params.buffer, params.byteOffset, params.byteLength);
    let offset = view.getUint32(0, true);
    let remaining = view.getUint32(4, true);
    const packetSize = view.getUint32(8, true);
    // Browser-only mock of the stub streaming protocol. Never touches hardware.
    this.transport.read = async () => {
      if (globalThis.__readFault) throw Object.assign(new Error('fixture serial failure'),
        {name:'SerialFault',kind:globalThis.__readFault});
      if (globalThis.__pauseFlashRead && !globalThis.__continueFlashRead) {
        await new Promise(resolve => { globalThis.__continueFlashRead = resolve; });
      }
      const length = Math.min(packetSize, remaining);
      if (length <= 0) throw new Error('unexpected extra flash packet');
      const data = new Uint8Array(length).fill(255);
      if (offset === (globalThis.__partitionAddress ?? 0x8000) && length >= 64) {
        const v = new DataView(data.buffer);
        v.setUint16(0,0x50aa,true);
        v.setUint32(4,0x10000,true);v.setUint32(8,0x20000,true);
        data.fill(0,12,32);data.set(new TextEncoder().encode('factory'),12);
      }
      offset += length;
      remaining -= length;
      return data;
    };
    this.transport.write = async () => {};
    return 0;
  }
  async readFlashId() { return 0x164068; }
  async readFlash(address, size, onPacketReceived) {
    if (globalThis.__readFault) throw Object.assign(new Error('fixture private transport detail'), { name: 'SerialFault', kind: globalThis.__readFault });
    if (globalThis.__pauseFlashRead) {
      onPacketReceived?.(new Uint8Array(), 0, size);
      await new Promise(resolve => { globalThis.__continueFlashRead = resolve; });
    }
    const bytes = new Uint8Array(size).fill(255);
    if (address === (globalThis.__partitionAddress ?? 0x8000) && size >= 64) {
      const view = new DataView(bytes.buffer); view.setUint16(0, 0x50aa, true); bytes[2] = 0; bytes[3] = 0;
      view.setUint32(4, 0x10000, true); view.setUint32(8, 0x20000, true);
      bytes.fill(0, 12, 32); bytes.set(new TextEncoder().encode('factory'), 12);
    }
    onPacketReceived?.(bytes, bytes.length, size);
    return bytes;
  }
}

// Test doubles, no board: a Transport with esptool-js 0.7.0's buffer contract and a scripted loader.
export class ESPError extends Error { constructor(m) { super(m); this.name = "ESPError"; } }

export class FakeTransport {
  constructor(device, tracing = false) {
    this.device = device; this.tracing = tracing;
    this.SLIP_END = 0xc0; this.SLIP_ESC = 0xdb; this.SLIP_ESC_END = 0xdc; this.SLIP_ESC_ESC = 0xdd;
    this.buffer = new Uint8Array(0);
  }
  appendArray(a, b) { const c = new Uint8Array(a.length + b.length); c.set(a); c.set(b, a.length); return c; }
  detectPanicHandler() {}
  trace() {}
  async readLoop() { await this.device.pumpDone; }          // ends when the fake port closes
  async disconnect() {}
  // what the chip sends: a SLIP-framed response
  static frame(bytes) { const o = [0xc0]; for (const b of bytes) { if (b === 0xdb) o.push(0xdb, 0xdd); else if (b === 0xc0) o.push(0xdb, 0xdc); else o.push(b); } o.push(0xc0); return Uint8Array.from(o); }
}
// the part of readLoop that feeds the buffer
export const feed = (t, bytes) => { t.buffer = t.appendArray(t.buffer, bytes); };
export const response = (op) => FakeTransport.frame([1, op, 2, 0, 0, 0, 0, 0, 0, 0]);   // resp=1, op, len 2, value 0, status 0 0

/** Scripted loader: the same method names as ESPLoader. `script[name]` is called with the arguments. */
export function fakeLoader(script = {}) {
  const calls = [];
  const sent = (n, ...a) => { calls.push([n, ...a]); return script[n] ? script[n](...a) : undefined; };
  const l = {
    calls,
    flashDeflBegin: async (...a) => sent("flashDeflBegin", ...a),
    flashBegin: async (...a) => sent("flashBegin", ...a),
    flashDeflBlock: async (...a) => sent("flashDeflBlock", ...a),
    flashBlock: async (...a) => sent("flashBlock", ...a),
    flashDeflFinish: async (...a) => sent("flashDeflFinish", ...a),
    flashFinish: async (...a) => sent("flashFinish", ...a),
    eraseFlash: async (...a) => sent("eraseFlash", ...a),
    flashMd5sum: async (...a) => sent("flashMd5sum", ...a),
    // mimics the order of ESPLoader.writeFlash 0.7.0 (stub, compress): begin, blocks, finish, md5
    async writeFlash(o) {
      for (const f of o.fileArray) {
        const md5 = o.calculateMD5Hash ? o.calculateMD5Hash(f.data) : null;
        await l.flashDeflBegin(f.data.length, f.data.length, f.address);
        await l.flashDeflBlock(f.data, 0, 5000);
        await l.flashDeflFinish(false, 3000);
        if (md5) {
          const got = await l.flashMd5sum(f.address, f.data.length);
          if (String(got) !== md5) throw new ESPError("MD5 of file does not match data in flash!");
        }
      }
    },
  };
  return l;
}

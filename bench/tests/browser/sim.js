// Simulated ESP32-S3 behind a fake Web Serial port, injected before the bench page loads (Firefox has no Web Serial).
// The real page and the real esptool-js 0.7.0 run unchanged on top of it. Nothing here touches a device.
// What it answers: SYNC (as a running stub), GET_SECURITY_INFO, READ_REG/WRITE_REG, SPI_ATTACH/SET_PARAMS, CHANGE_BAUDRATE,
// FLASH_DEFL_BEGIN/DATA/END (real zlib inflate), SPI_FLASH_MD5, ERASE_FLASH, READ_FLASH. Faults are scripted from the driver.
(() => {
  const realSetTimeout = window.setTimeout.bind(window);
  const enc = new TextEncoder();
  const S = (window.__sim = {
    flash: new Uint8Array(8 << 20).fill(0xff),
    ops: [],                       // persisted (sessionStorage): erase/write history, replayed after a page reload
    cmds: [],                      // every command the "chip" received: {t, op, name}
    sig: [],                       // setSignals calls
    opens: 0, closes: 0, mode: "app",
    cfg: { beginDelayMs: 0, beginDrop: false, closeOnBegin: false, closeAtBlock: -1, md5Drop: false, md5Corrupt: false, md5DelayMs: 0, blockDelayMs: 0, noSync: false },
    throttle: false, throttleStep: 1000,
    t0: performance.now(),
    log(...a) { S.cmds.push({ t: +(performance.now() - S.t0).toFixed(1), ...a[0] }); },
  });

  // ---- persistence of the flash content across a reload (sparse: only what was erased or written)
  const b64 = (u8) => { let s = ""; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); };
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  const save = () => { try { sessionStorage.setItem("__simflash", JSON.stringify(S.ops)); } catch (e) {} };
  try {
    S.ops = JSON.parse(sessionStorage.getItem("__simflash") || "[]");
    for (const o of S.ops) o.t === "erase" ? S.flash.fill(0xff, o.o, o.o + o.n) : S.flash.set(unb64(o.b), o.o);
  } catch (e) { S.ops = []; }
  S.eraseRange = (o, n) => { S.flash.fill(0xff, o, o + n); S.ops.push({ t: "erase", o, n }); save(); };
  S.writeRange = (o, bytes) => { S.flash.set(bytes, o); S.ops.push({ t: "write", o, b: b64(bytes) }); save(); };
  S.preload = (o, bytes) => S.writeRange(o, bytes);
  S.sha256 = async (o, n) => [...new Uint8Array(await crypto.subtle.digest("SHA-256", S.flash.slice(o, o + n)))].map((b) => b.toString(16).padStart(2, "0")).join("");

  // ---- timers: optionally behave like a throttled background tab (wake-ups only on a coarse grid)
  window.setTimeout = function (fn, d, ...a) {
    if (S.throttle) { const now = performance.now(); d = Math.ceil((now + (d || 0)) / S.throttleStep) * S.throttleStep - now; }
    return realSetTimeout(fn, d, ...a);
  };
  const later = (ms, fn) => (ms > 0 ? realSetTimeout(fn, ms) : fn());   // the chip's own clock is never throttled

  // ---- SLIP
  const slip = (payload) => {
    const out = [0xc0];
    for (const b of payload) b === 0xc0 ? out.push(0xdb, 0xdc) : b === 0xdb ? out.push(0xdb, 0xdd) : out.push(b);
    out.push(0xc0);
    return Uint8Array.from(out);
  };
  const u32 = (v) => [v & 255, (v >>> 8) & 255, (v >>> 16) & 255, (v >>> 24) & 255];
  const rd32 = (b, o) => (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0;

  let md5mod = null;
  const md5bytes = async (bytes) => {
    md5mod = md5mod || (await import("/md5.js"));
    const hex = md5mod.md5hex(bytes);
    return Uint8Array.from(hex.match(/../g).map((h) => parseInt(h, 16)));
  };
  const inflate = async (u8) => new Uint8Array(await new Response(new Blob([u8]).stream().pipeThrough(new DecompressionStream("deflate"))).arrayBuffer());

  // ---- the port
  class FakePort extends EventTarget {
    constructor() {
      super();
      this._open = false; this.readable = null; this.writable = null; this._ctl = null;
      this._dtr = false; this._rts = false; this._io0 = false; this._rx = []; this._inFrame = false; this._esc = false;
      this._def = null;   // compressed write in progress {offset, blocks, size, parts[]}
    }
    getInfo() { return { usbVendorId: 0x1a86, usbProductId: 0x55d3 }; }
    async open({ baudRate } = {}) {
      if (this._open) throw new DOMException("The port is already open.", "InvalidStateError");
      this._open = true; this.baud = baudRate; S.opens++;
      this.readable = new ReadableStream({ start: (c) => { this._ctl = c; }, cancel: () => { this._ctl = null; } });
      this.writable = new WritableStream({ write: (chunk) => this._rxBytes(chunk) });
      S.log({ ev: "open", baud: baudRate, mode: S.mode });
      // Opening the UART bridge resets the board the way the real one does (DTR/RTS pulse): the app boots again.
      if (baudRate === 115200) { S.mode = "app"; this._def = null; later(5, () => this._text(S.bootText || "I (31) boot: ESP-IDF v5.x 2nd stage bootloader\r\nI (310) app: iobewi ready\r\n")); }
    }
    async close() {
      if (!this._open) return;
      if ((this.readable && this.readable.locked) || (this.writable && this.writable.locked)) throw new TypeError("Cannot close a port with locked streams");
      this._open = false; this.readable = null; this.writable = null; this._ctl = null; S.closes++;
      S.log({ ev: "close" });
    }
    async setSignals(s = {}) {
      const was = this._rts;
      if ("dataTerminalReady" in s) this._dtr = !!s.dataTerminalReady;
      if ("requestToSend" in s) this._rts = !!s.requestToSend;
      S.sig.push({ t: +(performance.now() - S.t0).toFixed(1), dtr: this._dtr, rts: this._rts });
      if (was && !this._rts) {   // EN released: boots according to IO0 (DTR high = IO0 low = download mode)
        S.mode = this._dtr ? "rom" : "app"; this._def = null;
        later(5, () => this._text(S.mode === "rom"
          ? "ESP-ROM:esp32s3-20210327\r\nrst:0x1 (POWERON),boot:0x0 (DOWNLOAD(USB/UART0))\r\nwaiting for download\r\n"
          : (S.bootText || "I (31) boot: ESP-IDF v5.x 2nd stage bootloader\r\nI (310) app: iobewi ready\r\n")));
      }
    }
    // ---- chip → host
    _push(u8) { if (this._ctl) { try { this._ctl.enqueue(u8); } catch (e) {} } }
    _text(s) { this._push(enc.encode(s)); }
    _reply(cmd, val, data) { this._push(slip(Uint8Array.from([1, cmd, ...[data.length & 255, data.length >> 8], ...u32(val), ...data]))); }
    _ok(cmd, val = 0, extra = []) { this._reply(cmd, val, [...extra, 0, 0]); }
    // ---- fault: the USB device vanishes (streams error, 'disconnect' fires on navigator.serial with target = this port)
    vanish() {
      S.log({ ev: "vanish" });
      try { this._ctl && this._ctl.error(new DOMException("The device has been lost.", "NetworkError")); } catch (e) {}
      this._ctl = null; this._open = false; this.readable = null; this.writable = null;   // a lost device is a closed port
      const ev = new Event("disconnect"); Object.defineProperty(ev, "target", { value: this });
      navigator.serial.dispatchEvent(ev);
    }
    // ---- host → chip
    _rxBytes(chunk) {
      for (const b of chunk) {
        if (b === 0xc0) {
          if (this._inFrame && this._rx.length) { const f = Uint8Array.from(this._rx); this._rx = []; this._frame(f); }
          this._inFrame = true; this._rx = []; this._esc = false;
        } else if (this._inFrame) {
          if (this._esc) { this._rx.push(b === 0xdc ? 0xc0 : b === 0xdd ? 0xdb : b); this._esc = false; }
          else if (b === 0xdb) this._esc = true;
          else this._rx.push(b);
        }
      }
    }
    _frame(f) {
      if (S.mode !== "rom" || f.length < 8 || f[0] !== 0) return;   // 4-byte acks of a read-back, noise
      const op = f[1], len = f[2] | (f[3] << 8), d = f.subarray(8, 8 + len);
      S.log({ ev: "cmd", op, len });
      this._cmd(op, d, f).catch((e) => S.log({ ev: "sim-error", msg: String(e) }));
    }
    async _cmd(op, d) {
      switch (op) {
        case 0x08: if (S.cfg.noSync) return; for (let i = 0; i < 8; i++) this._reply(0x08, 0, [0, 0]); return;       // answers like a running stub
        case 0x14: return this._ok(op, 0, [...u32(0), 0, 0, 0, 0, 0, 0, 0, 0, ...u32(9), ...u32(0)]);   // security info: chip id 9 = ESP32-S3
        case 0x0a: { const a = rd32(d, 0); return this._ok(op, a === 0x60002058 ? 0x1640ef : 0); }       // SPI W0 = flash id
        case 0x09: case 0x0b: case 0x0d: return this._ok(op);
        case 0x0f: return this._ok(op);
        case 0x10: return this._deflBegin(op, d);
        case 0x11: return this._deflData(op, d);
        case 0x12: return this._ok(op);
        case 0x13: return this._md5(op, d);
        case 0xd0: S.eraseRange(0, S.flash.length); return this._ok(op);
        case 0xd2: return this._readFlash(op, d);
      }
      this._reply(op, 0, [1, 0x05]);   // not supported
    }
    _deflBegin(op, d) {
      const size = rd32(d, 0), blocks = rd32(d, 4), offset = rd32(d, 12);
      // The stub erases the region BEFORE it acknowledges: this is the point after which the flash may have changed.
      S.eraseRange(offset, Math.ceil(size / 4096) * 4096);
      this._def = { offset, blocks, size, parts: [] };
      S.log({ ev: "begin", offset, size, blocks });
      if (S.cfg.closeOnBegin) { later(50, () => this.vanish()); return; }
      if (S.cfg.beginDrop) return;
      later(S.cfg.beginDelayMs, () => this._ok(op));
    }
    _deflData(op, d) {
      const def = this._def; if (!def) return this._reply(op, 0, [1, 0x06]);
      const n = rd32(d, 0), seq = rd32(d, 4), payload = d.slice(16, 16 + n);
      if (S.cfg.closeAtBlock === seq) { S.log({ ev: "close-at-block", seq }); later(10, () => this.vanish()); return; }
      def.parts.push(payload);
      later(S.cfg.blockDelayMs, async () => {
        if (seq === def.blocks - 1) {
          const all = new Uint8Array(def.parts.reduce((a, p) => a + p.length, 0)); let o = 0;
          for (const p of def.parts) { all.set(p, o); o += p.length; }
          try { const raw = await inflate(all); S.writeRange(def.offset, raw.subarray(0, def.size)); S.log({ ev: "written", offset: def.offset, n: raw.length }); }
          catch (e) { return this._reply(op, 0, [1, 0x09]); }
        }
        this._ok(op);
      });
    }
    async _md5(op, d) {
      const a = rd32(d, 0), n = rd32(d, 4);
      S.log({ ev: "md5", a, n });
      if (S.cfg.md5Drop) return;
      let m = await md5bytes(S.flash.subarray(a, a + n));
      if (S.cfg.md5Corrupt) m = m.map((x, i) => (i === 0 ? x ^ 1 : x));
      later(S.cfg.md5DelayMs, () => this._reply(op, 0, [...m, 0, 0]));
    }
    _readFlash(op, d) {
      const a = rd32(d, 0), n = rd32(d, 4), bs = rd32(d, 8) || 4096;
      this._ok(op);
      for (let o = 0; o < n; o += bs) this._push(slip(S.flash.subarray(a + o, a + Math.min(n, o + bs))));
    }
  }

  const port = new FakePort();
  const serial = new EventTarget();
  serial.requestPort = async () => port;
  serial.getPorts = async () => (S.authorized !== false ? [port] : []);
  S.port = port;
  Object.defineProperty(navigator, "serial", { value: serial, configurable: true });
})();

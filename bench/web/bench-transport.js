// Serial transport for the bench: esptool-js Transport with a read() that cannot lose an answer.
//
// esptool-js 0.7.0 polls its buffer with `await sleep(1)` until `Date.now() - t0 < timeout` stops holding,
// then gives up WITHOUT looking at the buffer again. In a hidden or throttled tab the timer fires late: an
// answer that arrived in time is declared missing ("Serial data stream stopped: Possible serial noise or
// corruption", which is the no-byte-received message, not a noise detection).
// Here: the buffer is always examined first, the wait is woken by the arrival of data (the timer only bounds
// it), the deadline is monotonic, and the failure says what happened.
//
//   no-response     no byte within the limit (nothing says the chip did not act on the request)
//   partial-packet  bytes arrived, the packet never completed
//   invalid-packet  SLIP framing error (noise)
//   closed          the port ended or was lost (device lost, abort(), disconnect())
const EMPTY = new Uint8Array(0);

export function makeBenchTransport(Transport, ESPError, env = {}) {
  const now = env.now || (() => performance.now());
  const setT = env.setTimeout || ((f, ms) => setTimeout(f, ms));
  const clearT = env.clearTimeout || ((id) => clearTimeout(id));

  class SerialFault extends ESPError {
    constructor(kind, message, detail = {}) { super(message); this.name = "SerialFault"; this.kind = kind; this.detail = detail; }
  }

  class BenchTransport extends Transport {
    // `buffer` is what esptool-js fills (readLoop) and empties; the accessor wakes a waiting read().
    get buffer() { return this._buf || EMPTY; }
    set buffer(v) {
      const grew = v.length - (this._buf ? this._buf.length : 0);
      this._buf = v;
      if (grew > 0) { this._arrived(grew); this._wake(); }
    }
    _init() {
      if (this._ready) return;
      this._ready = true; this._waiters = new Set(); this._closed = null; this.onEvent = this.onEvent || null;
      this.stats = { reads: 0, readMs: 0, timeouts: 0, bytesIn: 0, closes: 0 };
    }
    _ev(name, detail) { if (this.onEvent) { try { this.onEvent(name, detail); } catch (e) {} } }
    _arrived(n) { this._init(); this.stats.bytesIn += n; this._firstByteAt = this._firstByteAt ?? now(); }
    _wake() { for (const w of [...this._waiters]) w(); }
    _take() { const b = this._buf || EMPTY; this._buf = EMPTY; return b; }
    flushInput() { this._buf = EMPTY; }

    // esptool-js changes the baud rate by closing and reopening the port (disconnect(), connect(), readLoop()): the
    // reopened session starts open. Each session has a number; the end of a previous session's read loop (it finishes
    // after the reopen has begun) must not close the new one.
    async connect(...a) {
      this._init();
      this._gen = (this._gen || 0) + 1;
      this._closed = null;
      return super.connect(...a);
    }
    async readLoop() {
      this._init();
      const gen = this._gen || 0;
      try { await super.readLoop(); }
      finally { if ((this._gen || 0) === gen) this.abort("closed", "port series terminé (flux de lecture clos)"); }
    }
    // Ends every pending and future read() with a `closed` fault. Called on USB loss and on disconnect.
    abort(kind = "closed", message = "port fermé") {
      this._init();
      if (!this._closed) { this._closed = { kind, message, at: now() }; this.stats.closes++; this._ev("port-closed", { kind, message }); }
      this._wake();
    }
    async disconnect() { this.abort("closed", "port fermé par disconnect()"); return super.disconnect(); }

    _waitData(ms) {
      return new Promise((resolve) => {
        const done = () => { clearT(timer); this._waiters.delete(done); resolve(); };
        const timer = setT(done, Math.max(0, ms));
        this._waiters.add(done);
      });
    }

    /** One SLIP packet. `timeout` bounds the silence (no byte at all), restarted after each received chunk. */
    async read(timeout) {
      this._init();
      const t0 = now();
      this.stats.reads++;
      this._ev("read-start", { timeout });
      let partial = null, escaping = false, deadline = t0 + timeout, got = 0;
      const finish = (outcome) => { this.stats.readMs += now() - t0; this._ev("read-end", { outcome, ms: now() - t0, bytes: got }); };
      for (;;) {
        const chunk = this._take();
        if (chunk.length === 0) {
          if (this._closed) { finish("closed"); throw new SerialFault("closed", "port série fermé pendant la lecture : " + this._closed.message); }
          const left = deadline - now();
          if (left <= 0) {
            this.stats.timeouts++; finish("timeout");
            this._ev("timeout", { limit: timeout, partial: partial !== null });
            throw new SerialFault(partial === null ? "no-response" : "partial-packet",
              partial === null ? `réponse absente : aucun octet reçu en ${timeout} ms` : `réponse incomplète : paquet commencé, fin non reçue en ${timeout} ms`,
              { limit: timeout });
          }
          await this._waitData(left);
          continue;
        }
        if (got === 0) this._ev("first-byte", { after: now() - t0 });
        got += chunk.length;
        deadline = now() + timeout;
        for (let i = 0; i < chunk.length; i++) {
          const byte = chunk[i];
          if (partial === null) {
            if (byte !== this.SLIP_END) {
              this._buf = this.appendArray(chunk.slice(i + 1), this._buf || EMPTY);
              this.detectPanicHandler(new Uint8Array([...chunk, ...(this._buf || EMPTY)]));
              finish("invalid-packet");
              throw new SerialFault("invalid-packet", `paquet invalide (octet 0x${byte.toString(16)} au lieu du délimiteur) : bruit possible`);
            }
            partial = new Uint8Array(0);
          } else if (escaping) {
            escaping = false;
            if (byte === this.SLIP_ESC_END) partial = this.appendArray(partial, new Uint8Array([this.SLIP_END]));
            else if (byte === this.SLIP_ESC_ESC) partial = this.appendArray(partial, new Uint8Array([this.SLIP_ESC]));
            else { finish("invalid-packet"); throw new SerialFault("invalid-packet", `séquence SLIP invalide (0xdb, 0x${byte.toString(16)})`); }
          } else if (byte === this.SLIP_ESC) escaping = true;
          else if (byte === this.SLIP_END) {
            if (i + 1 < chunk.length) this._buf = this.appendArray(chunk.slice(i + 1), this._buf || EMPTY);
            finish("packet");
            return partial;
          } else partial = this.appendArray(partial, new Uint8Array([byte]));
        }
      }
    }
  }
  return { BenchTransport, SerialFault };
}

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
import { latencyStats, observeLatency, summarizeLatency } from './flash-read-latency.js';

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
    // Counters only: no changes to the flash protocol, ACKs, retries or timeouts.
    beginFlashReadProfile() {
      this.flashReadProfile = { packets: 0, payload_bytes: 0, read_ms: 0,
        waiting_ms: 0, write_calls: 0, write_ms: 0,
        packet_interval: latencyStats(), ack_to_first_byte: latencyStats(),
        read_to_first_byte: latencyStats(), frame_receive: latencyStats(),
        lastPacketAt: null, lastAckAt: null };
    }
    endFlashReadProfile() {
      const profile = this.flashReadProfile || null;
      this.flashReadProfile = null;
      if (!profile) return null;
      return { packets: profile.packets, payload_bytes: profile.payload_bytes,
        read_ms: Math.round(profile.read_ms), waiting_ms: Math.round(profile.waiting_ms),
        write_calls: profile.write_calls, write_ms: Math.round(profile.write_ms),
        timing: {
          packet_interval: profile.packet_interval,
          ack_to_first_byte: profile.ack_to_first_byte,
          read_to_first_byte: profile.read_to_first_byte,
          frame_receive: profile.frame_receive,
        } };
    }
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

    // esptool-js 0.7.0 does not release the writer when an ACK write fails.
    // In particular USB loss during flash reading must not leave the port locked.
    async write(data) {
      this._init();
      if (this._closed || !this.device?.writable) throw new SerialFault('closed', 'serial output unavailable');
      const writer = this.device.writable.getWriter();
      const t0 = this.flashReadProfile ? now() : null;
      try {
        await writer.write(this.slipWriter(data));
        // During ESP_READ_FLASH, esptool-js sends a 4-byte cumulative ACK.
        // This timestamp marks completion of its Web Serial write, not USB wire delivery.
        if (data.length === 4 && this.flashReadProfile) this.flashReadProfile.lastAckAt = now();
      }
      catch (error) {
        if (error?.name === 'NetworkError') { this.abort('closed', 'serial write interrupted'); throw new SerialFault('closed', 'serial write interrupted'); }
        throw error;
      } finally {
        if (t0 !== null && this.flashReadProfile) {
          this.flashReadProfile.write_calls++;
          this.flashReadProfile.write_ms += now() - t0;
        }
        writer.releaseLock();
      }
    }

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
      let partial = null, length = 0, escaping = false, deadline = t0 + timeout, got = 0;
      let firstByteAt = null;
      const append = byte => {
        if (length === partial.length) { const grown = new Uint8Array(Math.max(256, length * 2)); grown.set(partial); partial = grown; }
        partial[length++] = byte;
      };
      const finish = (outcome) => {
        const elapsed = now() - t0;
        this.stats.readMs += elapsed;
        if (this.flashReadProfile) {
          this.flashReadProfile.read_ms += elapsed;
          if (outcome === 'packet') {
            this.flashReadProfile.packets++;
            this.flashReadProfile.payload_bytes += length;
            if (firstByteAt !== null) observeLatency(this.flashReadProfile.frame_receive, now() - firstByteAt);
          }
        }
        this._ev("read-end", { outcome, ms: elapsed, bytes: got });
      };
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
          const waitStart = this.flashReadProfile ? now() : null;
          try { await this._waitData(left); }
          finally { if (waitStart !== null && this.flashReadProfile) this.flashReadProfile.waiting_ms += now() - waitStart; }
          continue;
        }
        if (got === 0) {
          firstByteAt = now();
          const p = this.flashReadProfile;
          if (p) {
            observeLatency(p.read_to_first_byte, firstByteAt - t0);
            if (p.lastPacketAt !== null) observeLatency(p.packet_interval, firstByteAt - p.lastPacketAt);
            if (p.lastAckAt !== null) observeLatency(p.ack_to_first_byte, Math.max(0, firstByteAt - p.lastAckAt));
            p.lastPacketAt = firstByteAt;
            p.lastAckAt = null;
          }
          this._ev("first-byte", { after: firstByteAt - t0 });
        }
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
            partial = new Uint8Array(256); length = 0;
          } else if (escaping) {
            escaping = false;
            if (byte === this.SLIP_ESC_END) append(this.SLIP_END);
            else if (byte === this.SLIP_ESC_ESC) append(this.SLIP_ESC);
            else { finish("invalid-packet"); throw new SerialFault("invalid-packet", `séquence SLIP invalide (0xdb, 0x${byte.toString(16)})`); }
          } else if (byte === this.SLIP_ESC) escaping = true;
          else if (byte === this.SLIP_END) {
            if (i + 1 < chunk.length) this._buf = this.appendArray(chunk.slice(i + 1), this._buf || EMPTY);
            finish("packet");
            return partial.slice(0, length);
          } else append(byte);
        }
      }
    }
  }
  return { BenchTransport, SerialFault };
}


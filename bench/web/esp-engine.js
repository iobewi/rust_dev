// Hardware only: no DOM, REST, project readiness rules or verdicts.
import { makeBenchTransport } from './bench-transport.js';
import { runWrite, Tracer } from './flash-op.js';
import { b64, unb64, family, region, validateImages, partitions, sha256 } from './protocol.js';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export class EspEngine {
  constructor(port, deps, event, hold = () => {}) {
    this.port = port; this.deps = deps; this.event = event; this.persistHold = hold;
    this.native = port.getInfo().usbVendorId === 0x303a;
    this.mode = 'off'; this.reader = null; this.loader = null; this.baudrate = 115200;
    this.flashBaud = 460800; this.hold = false; this.trigger = null; this.tail = new Uint8Array();
    this.trace = new Tracer(); this.busy = false;
  }
  setHold(value) { this.hold = value; this.persistHold(value); }
  capabilities() {
    const caps = ['identify', 'partitions', 'flash.read', 'flash.write', 'flash.erase', 'uart.write', 'uart.configure', 'console', 'mark', 'trigger', 'http.request'];
    return this.native ? caps : [...caps, 'reset', 'io0'];
  }
  async openConsole() {
    if (this.hold) throw new Error('Operator recovery required before opening console');
    if (this.mode === 'console') return;
    if (this.transport) { await this.transport.disconnect(); this.transport = null; this.loader = null; }
    await this.port.open({ baudRate: this.baudrate });
    await this.port.setSignals({ dataTerminalReady: false, requestToSend: false });
    this.mode = 'console'; this.readDone = this.readLoop();
    this.event('transport', { detail: 'console opened; opening a port may reset the MCU' });
  }
  async readLoop() {
    this.reader = this.port.readable.getReader();
    try {
      for (;;) {
        const { value, done } = await this.reader.read(); if (done) break;
        this.event('uart', { data: b64(value), at: Date.now() });
        this.matchTrigger(value);
      }
    } catch { this.event('transport', { detail: 'UART interrupted' }); }
    finally { this.reader.releaseLock(); this.reader = null; }
  }
  async closeConsole() {
    if (this.mode !== 'console') return;
    this.mode = 'off';
    if (this.reader) await this.reader.cancel();
    await this.readDone;
    await this.port.close();
  }
  async close() {
    this.trigger = null;
    await this.closeConsole();
    if (this.transport) { await this.transport.disconnect(); this.transport = null; this.loader = null; }
    this.mode = 'off';
  }
  async openLoader() {
    if (this.loader) return this.loader;
    await this.closeConsole();
    const { ESPLoader, Transport, ESPError } = this.deps;
    const { BenchTransport } = makeBenchTransport(Transport, ESPError);
    this.transport = new BenchTransport(this.port, false);
    this.transport.onEvent = (name, detail) => this.trace.ev(name, detail);
    const terminal = { clean() {}, write() {}, writeLine: () => {} };
    this.loader = new ESPLoader({ transport: this.transport, baudrate: this.flashBaud, terminal });
    try { this.chipDescription = await this.loader.main(); this.mode = 'loader'; }
    catch (e) { await this.close(); throw e; }
    return this.loader;
  }
  async identity() {
    const l = await this.openLoader(), id = await l.readFlashId();
    const code = id >>> 16 & 255;
    if (code < 12 || code > 26) throw new Error('Unknown flash capacity');
    const chip = family(this.chipDescription.replace(/\s*\(.*$/, ''));
    return { chip, flash_size: 2 ** code, flash_id: id, mac: await l.chip.readMac(l) };
  }
  async reset() {
    if (this.native) throw new Error('Native USB has no EN line');
    if (this.mode !== 'console') throw new Error('Reset requires console mode');
    await this.port.setSignals({ dataTerminalReady: false, requestToSend: true });
    try { await sleep(100); } finally { await this.port.setSignals({ dataTerminalReady: false, requestToSend: false }); }
  }
  matchTrigger(chunk) {
    const trigger = this.trigger;
    if (!trigger) return;
    if (performance.now() >= trigger.until || !trigger.live()) { this.trigger = null; return; }
    const bytes = new Uint8Array(this.tail.length + chunk.length); bytes.set(this.tail); bytes.set(chunk, this.tail.length);
    let matched = false;
    outer: for (let i = 0; i <= bytes.length - trigger.bytes.length; i++) {
      for (let j = 0; j < trigger.bytes.length; j++) if (bytes[i + j] !== trigger.bytes[j]) continue outer;
      matched = true; break;
    }
    this.tail = bytes.slice(-Math.max(0, trigger.bytes.length - 1));
    if (!matched) return;
    this.trigger = null;
    this.event('trigger', { detail: 'one-shot byte pattern matched' });
    setTimeout(async () => {
      // A delayed trigger must never collide with flash, another reservation, or a disconnected server.
      if (!trigger.live() || this.hold || performance.now() >= trigger.until) return;
      if (trigger.action === 'reset' && (this.busy || this.mode !== 'console')) return;
      try { if (trigger.action === 'reset') await this.reset(); else this.event('mark', { detail: 'trigger' }); }
      catch { this.event('transport', { detail: 'trigger action failed' }); }
    }, trigger.delay);
  }
  async execute(command, args, context) {
    if (this.busy) throw new Error('Device is busy');
    if (this.hold && !['identify', 'partitions', 'flash.read', 'flash.write', 'flash.erase', 'mark'].includes(command)) throw new Error('Operator recovery required');
    this.busy = true;
    let destructive = false;
    try {
      context.check();
      if (command === 'mark') { this.event('mark', { detail: args.label }); return { label: args.label }; }
      if (command === 'trigger') {
        if (args.action === 'disarm') { this.trigger = null; return { armed: false }; }
        if (args.action === 'reset' && this.native) throw new Error('No EN line');
        this.trigger = { bytes: unb64(args.data), action: args.action, until: performance.now() + args.expires_ms,
          delay: args.delay_ms || 0, live: context.live };
        this.tail = new Uint8Array(); return { armed: true };
      }
      if (command === 'http.request') {
        const url = new URL(args.url);
        const octets = url.hostname.split('.').map(Number);
        const privateIp = octets.length === 4 && octets.every(n => Number.isInteger(n) && n >= 0 && n <= 255) &&
          (octets[0] === 10 || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) || (octets[0] === 192 && octets[1] === 168));
        if (!privateIp || url.protocol !== 'http:' || url.username || url.password || !context.device.http_origins?.includes(url.origin)) throw new Error('HTTP origin not allowed');
        const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), args.timeout_ms || 15000);
        try {
          await fetch(url, { method: args.method || 'GET', mode: 'no-cors', redirect: 'error', credentials: 'omit',
            referrerPolicy: 'no-referrer', cache: 'no-store', signal: ctl.signal,
            ...(args.method === 'POST' ? { headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: args.body || '' } : {}) });
          return { response: 'opaque', http_status: 'unknown', verified: false };
        } finally { clearTimeout(timer); }
      }
      if (command === 'console') { await this.openConsole(); return { mode: this.mode }; }
      if (command === 'reset') { await this.reset(); return { reset: true }; }
      if (command === 'io0') {
        if (this.native || this.mode !== 'console') throw new Error('IO0 requires external UART console');
        await this.port.setSignals({ dataTerminalReady: true, requestToSend: false });
        try { await sleep(args.duration_ms); } finally { await this.port.setSignals({ dataTerminalReady: false, requestToSend: false }); }
        return { duration_ms: args.duration_ms };
      }
      if (command === 'uart.configure') {
        await this.closeConsole(); this.baudrate = args.baudrate;
        await this.openConsole(); return { baudrate: this.baudrate };
      }
      if (command === 'uart.write') {
        if (this.mode !== 'console') throw new Error('UART console is not open');
        const writer = this.port.writable.getWriter();
        try { await writer.write(unb64(args.data)); } finally { writer.releaseLock(); }
        return { sent: unb64(args.data).length };
      }
      // Download and verify all firmware bytes before changing the boot state.
      const images = [];
      if (command === 'flash.write') {
        for (const i of args.images) {
          const data = await context.download(i.artifact);
          if (await sha256(data) !== i.sha256) throw new Error('Artifact SHA-256 mismatch');
          images.push({ ...i, data });
        }
      }
      context.check();
      const physical = await this.identity(), l = this.loader;
      if (command.startsWith('flash.') || command === 'partitions') {
        if (args.chip && args.chip !== physical.chip) throw new Error('Physical chip mismatch');
        if (physical.chip !== context.device.chip || physical.flash_size !== context.device.flash_size) throw new Error('Device identity changed');
      }
      context.check();
      let result;
      if (command === 'identify') result = physical;
      else if (command === 'partitions') {
        region(args.address ?? 0x8000, 0xc00, physical.flash_size);
        result = { partitions: partitions(await l.readFlash(args.address ?? 0x8000, 0xc00), physical.flash_size) };
      } else if (command === 'flash.read') {
        region(args.address, args.size, physical.flash_size);
        const data = await l.readFlash(args.address, args.size);
        if (data.length !== args.size) throw new Error('Short flash read');
        result = { artifact: await context.upload(data), address: args.address };
      } else if (command === 'flash.write') {
        validateImages(images, physical.flash_size); context.check();
        destructive = true; this.setHold(true);
        result = await runWrite(l, images.map(i => ({ data: i.data, addr: i.address, path: i.artifact, sha: i.sha256 })),
          { erase: args.erase === true, tracer: this.trace });
        if (!result.ok) return { ...result, hold: true };
        if (context.live()) this.setHold(false);
      } else if (command === 'flash.erase') {
        context.check(); destructive = true; this.setHold(true);
        await l.eraseFlash(); result = { erased: true };
        if (context.live()) this.setHold(false);
      } else throw new Error('Unsupported command');
      if (args.return_console !== false && !this.hold && context.live()) await this.openConsole();
      return result;
    } catch (e) {
      if (destructive) { this.setHold(true); return { state: 'indeterminate', hold: true, error: { code: 'hardware_outcome_unknown' }, trace: this.trace.snapshot().slice(-60) }; }
      throw e;
    } finally { this.busy = false; }
  }
}

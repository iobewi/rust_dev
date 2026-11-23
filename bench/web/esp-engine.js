import { effectiveLoaderBaud, flashTransfer } from './loader-settings.js';
// Hardware only: no DOM, REST, project readiness rules or verdicts.
import { HardwareError, hardwareError } from './hardware-errors.js';
import { makeBenchTransport } from './bench-transport.js';
import { readFlashPreallocated } from './flash-read-preallocated.js';
import { flashMd5Diagnostic, MD5_DIAGNOSTIC_MAX_SIZE } from './flash-md5-diagnostic.js';
import { latencyStats, mergeLatency, summarizeLatency } from './flash-read-latency.js';
import { runWrite, Tracer } from './flash-op.js';
import { b64, unb64, family, region, validateImages, partitions, sha256 } from './protocol.js';
import { P4_TRACE, runP4UsbTrace } from './p4-diagnostic.js';
import { P4_BRINGUP, runP4UsbBringup } from './p4-bringup.js';
import { P4_FLASHER_READ, runP4FlasherRead } from './p4-flasher-read.js';
import { observeSessionState, judgeSessionState, sessionStateReport } from './p4-session-state.js';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const latencyFields = ['packet_interval', 'ack_to_first_byte', 'read_to_first_byte', 'frame_receive',
  'raw_chunk_interval', 'ack_to_raw_chunk'];
function readProfileSnapshot(profile) {
  if (!profile) return null;
  const { timing, ...totals } = profile;
  return { ...totals, timing: Object.fromEntries(latencyFields.map(key => [key, summarizeLatency(timing[key])])) };
}

export class EspEngine {
  constructor(port, deps, event, hold = () => {}) {
    this.port = port; this.deps = deps; this.event = event; this.persistHold = hold;
    const usb = port.getInfo();
    this.native = usb.usbVendorId === 0x303a;
    this.usbSerialJtag = this.native && usb.usbProductId === 0x1001;
    this.mode = 'off'; this.reader = null; this.loader = null; this.baudrate = 115200;
    // A native USB port (303a:*, USB-Serial-JTAG or the ROM's USB-OTG, e.g. 303a:0012 on the ESP32-P4) has no UART speed: esptool-js's baud change after the stub (and the port reopening it implies) is
    // only for an external UART bridge, and made the loader unreachable on the P4's USB-OTG port.
    this.flashBaud = 115200; this.loaderBaud = null; this.manualDownload = false; this.romOnly = false; this.identityChecks = 0; this.hold = false; this.diagnosticResident = false; this.trigger = null; this.tail = new Uint8Array();
    this.trace = new Tracer(); this.busy = false; this.phase = 'prepare'; this.readProgress = {};
  }
  provenance() {
    const usb = this.port.getInfo();
    const transport = this.usbSerialJtag ? 'usb_serial_jtag' : this.native ? 'native_usb_other' :
      [0x10c4, 0x1a86, 0x0403, 0x067b].includes(usb.usbVendorId) ? 'external_uart' : 'unknown';
    return { usb_vid: usb.usbVendorId ?? null, usb_pid: usb.usbProductId ?? null,
      transport, transport_basis: 'inferred_from_webserial_vid_pid' };
  }
  diagnostics() {
    const stats = this.transport?.stats || this.lastTransportStats || {};
    const base = this.transport === this.baselineTransport ? this.serialBaseline || {} : {};
    return { phase: this.phase, ...this.readProgress,
      serial_reads: Math.max(0, (stats.reads || 0) - (base.reads || 0)),
      serial_bytes: Math.max(0, (stats.bytesIn || 0) - (base.bytesIn || 0)),
      serial_timeouts: Math.max(0, (stats.timeouts || 0) - (base.timeouts || 0)),
      ...(this.flashReadProfile ? { flash_read_profile: readProfileSnapshot(this.flashReadProfile) } : {}), ...this.lastRead };
  }
  setPhase(phase, context) { this.phase = phase; context.report?.(this.diagnostics()); }
  async readFlash(loader, address, size, context, reader = 'reference', packetSize = 4096) {
    const startedAt = performance.now(); this.lastFlashTransfer = null;
    this.flashReadProfile = { blocks: 0, elapsed_ms: 0, packets: 0, payload_bytes: 0,
      read_ms: 0, waiting_ms: 0, write_calls: 0, write_ms: 0,
      raw_chunks: 0, raw_bytes: 0, max_raw_chunk_bytes: 0,
      timing: Object.fromEntries(latencyFields.map(key => [key, latencyStats()])) };
    // Keep esptool-js's cumulative append limited to 1 MiB per command. No retry:
    // every block is read once and only a complete region becomes an artifact.
    const data = new Uint8Array(size), blockLimit = 1024 * 1024;
    let nextReport = 0;
    const check = () => {
      if (context.checkLive) context.checkLive();
      else if (context.live && !context.live()) throw new HardwareError('execution_deadline_exceeded');
    };
    for (let offset = 0; offset < size; offset += blockLimit) {
      check();
      const blockSize = Math.min(blockLimit, size - offset);
      this.readProgress = { received_bytes: offset, expected_bytes: size, block_address: address + offset, block_size: blockSize };
      const blockStarted = performance.now();
      this.transport?.beginFlashReadProfile?.();
      let block;
      try {
        const read = reader === 'preallocated' || reader === 'packet-diagnostic'
          ? (a, b, callback) => readFlashPreallocated(loader, a, b, callback, packetSize)
          : (a, b, callback) => loader.readFlash(a, b, callback);
        block = await read(address + offset, blockSize, (_packet, received) => {
        check();
        if (Number.isSafeInteger(received) && received >= 0 && received <= blockSize) this.readProgress.received_bytes = offset + received;
        if (this.readProgress.received_bytes >= nextReport) {
          context.report?.(this.diagnostics()); nextReport = this.readProgress.received_bytes + 256 * 1024;
        }
        });
      } finally {
        const transportMetrics = this.transport?.endFlashReadProfile?.();
        this.flashReadProfile.blocks++;
        this.flashReadProfile.elapsed_ms += Math.round(performance.now() - blockStarted);
        if (transportMetrics) {
          for (const key of ['packets', 'payload_bytes', 'read_ms', 'waiting_ms', 'write_calls', 'write_ms',
            'raw_chunks', 'raw_bytes'])
            this.flashReadProfile[key] += transportMetrics[key] || 0;
          this.flashReadProfile.max_raw_chunk_bytes = Math.max(this.flashReadProfile.max_raw_chunk_bytes,
            transportMetrics.max_raw_chunk_bytes || 0);
          for (const key of latencyFields) mergeLatency(this.flashReadProfile.timing[key], transportMetrics.timing?.[key]);
        }
      }
      if (!(block instanceof Uint8Array) || block.length !== blockSize) throw new HardwareError('short_flash_read');
      data.set(block, offset); this.readProgress.received_bytes = offset + blockSize;
      context.report?.(this.diagnostics()); check();
    }
    this.lastFlashTransfer = { ...flashTransfer(size, performance.now() - startedAt), reader, packet_size: packetSize, profile: readProfileSnapshot(this.flashReadProfile) };
    return data;
  }
  setHold(value) {
    if (value === false && this.diagnosticResident) {
      // Operator recovery after the diagnostic: every handle of the session that talked to the diagnostic stub is stale.
      this.diagnosticResident = false;
      if (this.loader?.IS_STUB === true || this.loader?.__idlabTraceStub || this.loader?.__idlabBringupStub) this.loader = null;   // a loader that spoke to the diagnostic stub is stale
    }
    this.hold = value; this.persistHold(value);
  }
  cancelTrigger() {
    clearTimeout(this.triggerTimer);
    this.triggerTimer = null; this.trigger = null; this.tail = new Uint8Array();
    this.triggerGeneration = (this.triggerGeneration || 0) + 1;
  }
  capabilities() {
    const caps = ['identify', 'partitions', 'flash.read', 'flash.write', 'flash.erase', 'uart.write', 'uart.configure', 'console', 'mark', 'trigger', 'http.request'];
    // ROM-only connection (explicit operator choice, native USB OTG P4 only): the standard stub is never loaded, so only the
    // ROM-level identification, markers and the P4 diagnostic exist in this session. Nothing that needs the stub is offered.
    if (this.romOnly === true) return this.native && !this.usbSerialJtag && /ESP32-P4/i.test(this.chipDescription || '') ? ['identify', 'mark', P4_TRACE, P4_BRINGUP, P4_FLASHER_READ] : ['identify', 'mark'];
    if (!this.native) return [...caps, 'reset', 'io0', 'boot.download'];
    return this.usbSerialJtag ? [...caps, 'reset'] : caps;
  }
  // Manual entry also supports a UART bridge with no DTR/RTS wiring. Only an
  // explicit console action may leave the loader after this operator opt-in.
  keepsLoaderOpen() { return this.manualDownload === true; }
  entryMode() { return this.keepsLoaderOpen() ? 'no_reset' : 'default_reset'; }
  async openConsole(explicit = false) {
    if (!explicit && this.keepsLoaderOpen() && this.mode === 'loader') return;       // only an explicit console command / uart.configure leaves the loader session
    if (this.hold) throw new HardwareError('operator_recovery_required');
    if (this.mode === 'console') return;
    this.event('transport', { detail: 'opening console' });
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
        for (let offset = 0; offset < value.length; offset += 16384) {
          this.event('uart', { data: b64(value.subarray(offset, offset + 16384)), at: Date.now() });
        }
        this.matchTrigger(value);
      }
    } catch { this.event('transport', { detail: 'UART interrupted' }); }
    finally { this.reader.releaseLock(); this.reader = null; }
  }
  async closeConsole() {
    if (this.mode !== 'console') return;
    this.mode = 'off';
    // USB loss can reject cancellation. Still drain the loop and close the real port.
    try { if (this.reader) await this.reader.cancel(); } catch { /* The loop releases its lock. */ }
    try { await this.readDone; } catch { /* Closing the port below remains mandatory. */ }
    await this.port.close();
  }
  async close() {
    this.cancelTrigger();
    await this.closeConsole();
    if (this.transport) { await this.transport.disconnect(); this.transport = null; this.loader = null; }
    // A failed/partial open may leave Web Serial open while the engine mode is off.
    if (this.port.readable || this.port.writable) await this.port.close();
    this.mode = 'off';
  }
  async openLoader() {
    if (this.loader) return this.loader;
    if (this.romOnly === true) return this.openRomSession();   // connect + detect only: never runStub()
    const baudrate = effectiveLoaderBaud(this.native, this.loaderBaud, this.flashBaud);
    await this.closeConsole();
    this.event('transport', { detail: 'opening loader' });
    const { ESPLoader, Transport, ESPError } = this.deps;
    const { BenchTransport } = makeBenchTransport(Transport, ESPError);
    this.transport = new BenchTransport(this.port, false);
    this.transport.onEvent = (name, detail) => {
      this.trace.ev(name, detail); this.lastTransportStats = this.transport?.stats;
      if (name === 'read-start') this.lastRead = { silence_limit_ms: detail.timeout };
      if (name === 'read-end') this.lastRead = { ...this.lastRead, last_read_ms: detail.ms, last_packet_bytes: detail.bytes };
    };
    const terminal = { clean() {}, write() {}, writeLine: () => {} };
    this.loader = new ESPLoader({ transport: this.transport, baudrate, terminal });
    this.identityChecks = 0; // Diagnostic rank scoped to this loader session.
    try {
      const entry = this.entryMode();
      if (entry === 'no_reset') this.event('transport', { detail: 'loader entry without reset (operator put the chip in ROM download mode)' });
      this.chipDescription = await this.loader.main(entry); this.mode = 'loader';
    }
    catch (e) { try { await this.close(); } catch { /* Preserve the original serial failure. */ } throw e; }
    return this.loader;
  }
  // ROM-only session: connect + detect, never runStub(), never a reset (no_reset entry). Used as THE connection of a board
  // added with "ROM seule" and by nothing else: a stub already running (esptool-js loads one in main()) cannot be unloaded.
  async openRomSession() {
    if (this.romOnly !== true) throw new HardwareError('diagnostic_rom_only_required');
    if (this.transport) { await this.transport.disconnect(); this.transport = null; this.loader = null; this.mode = 'off'; }
    await this.closeConsole();
    this.event('transport', { detail: 'opening ROM-only loader session (no reset, no stub)' });
    const { ESPLoader, Transport, ESPError } = this.deps;
    const { BenchTransport } = makeBenchTransport(Transport, ESPError);
    this.transport = new BenchTransport(this.port, false);
    this.transport.onEvent = (name, detail) => this.trace.ev(name, detail);
    const terminal = { clean() {}, write() {}, writeLine: () => {} };
    this.loader = new ESPLoader({ transport: this.transport, baudrate: 115200, terminal });
    this.identityChecks = 0;
    try {
      await this.loader.detectChip('no_reset');
      this.chipDescription = await this.loader.chip.getChipDescription(this.loader);
      this.mode = 'loader';
    } catch (e) { try { await this.close(); } catch { /* Preserve the original serial failure. */ } throw e; }
    return this.loader;
  }
  // Proof that no stub runs: the fresh ROM/stub detection of p4-session-state.js on the CURRENT ROM-only connection. It transmits
  // ESP_SYNC frames only (no reset, no RAM load, no NOR write; port left open). Only a `rom` verdict is a proof; `stub` and
  // `indeterminate` are refused with a structured error (the server then keeps the quarantine). A lost transport is not masked.
  async verifyRomReturn() {
    if (this.romOnly !== true) throw new HardwareError('diagnostic_rom_only_required');
    if (this.busy) throw new Error('Device is busy');
    this.busy = true;
    try {
      const loader = await this.openLoader();
      if (loader.IS_STUB === true) throw new HardwareError('diagnostic_stub_still_running');
      let evidence;
      try { evidence = await observeSessionState(loader); }
      catch (error) {
        const lost = ['closed', 'no-response', 'partial-packet', 'invalid-packet'].includes(error?.kind) || error?.name === 'NetworkError';
        if (lost) throw error;   // a disconnection / serial fault keeps its own code (serial_disconnected, serial_timeout, …)
        throw new HardwareError('diagnostic_recovery_sync_failed', { phase: 'control' });
      }
      const judged = judgeSessionState(evidence), sessionState = { phase: 'control', ...sessionStateReport(evidence, judged) };
      if (judged.verdict === 'stub') throw new HardwareError('diagnostic_stub_still_running', sessionState);
      if (judged.verdict !== 'rom') throw new HardwareError('diagnostic_recovery_state_indeterminate', sessionState);
      if (loader.chip?.CHIP_NAME !== 'ESP32-P4') throw new HardwareError('diagnostic_chip_unsupported');
      return true;
    } finally { this.busy = false; }
  }
  async identity() {
    const l = await this.openLoader();
    this.identityChecks += 1;
    if (this.romOnly === true) return this.romOnlyIdentity(l);
    const id = await l.readFlashId();
    const code = id >>> 16 & 255;
    if (code < 12 || code > 26) {
      throw new HardwareError('flash_capacity_unknown', {
        phase: 'identify', jedec_id: id >>> 0,
        identify_index: this.identityChecks, loader_stub: l.IS_STUB ? 1 : 0,
      });
    }
    const chip = family(this.chipDescription.replace(/\s*\(.*$/, ''));
    return { chip, flash_size: 2 ** code, flash_id: id, mac: await l.chip.readMac(l) };
  }
  // "ROM seule" only. The MCU identity (family + MAC) is mandatory and verified; the flash capacity is MEASURED from a valid
  // JEDEC id or declared unknown (flash_size null, flash_size_known false): it is never invented nor taken from a remembered
  // identity. The raw JEDEC id and its read context are kept (console event + `flash_capacity`), without assuming a cause.
  async romOnlyIdentity(l) {
    const chip = family(String(this.chipDescription || '').replace(/\s*\(.*$/, ''));
    if (chip !== 'esp32p4') {   // the unknown-capacity path exists for the P4 diagnostic only: any other chip keeps the strict rule
      const id = await l.readFlashId(), code = id >>> 16 & 255;
      if (code < 12 || code > 26) throw new HardwareError('flash_capacity_unknown', { phase: 'identify', jedec_id: id >>> 0, identify_index: this.identityChecks, loader_stub: l.IS_STUB ? 1 : 0 });
      return { chip, flash_size: 2 ** code, flash_id: id, mac: await l.chip.readMac(l) };
    }
    const mac = await l.chip.readMac(l);   // a failure here (communication or chip) refuses the connection
    if (typeof mac !== 'string' || !/^([0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(mac) || /^(00:){5}00$|^(ff:){5}ff$/i.test(mac))
      throw new HardwareError('physical_identity_unverified', { phase: 'identify' });
    let jedec = null, read = 'ok';
    try { jedec = await l.readFlashId(); }
    catch (error) {
      if (['closed', 'no-response', 'partial-packet', 'invalid-packet'].includes(error?.kind) || error?.name === 'NetworkError') throw error;   // a lost transport is not "unavailable"
      read = 'error';
    }
    const raw = Number.isInteger(jedec) ? jedec >>> 0 : null, code = raw === null ? -1 : raw >>> 16 & 255;
    const valid = code >= 12 && code <= 26;
    if (!valid && read === 'ok') read = 'invalid';
    const evidence = { jedec_id: raw, read, identify_index: this.identityChecks, loader_stub: l.IS_STUB ? 1 : 0, rom_only: true };
    this.flashCapacityEvidence = evidence;
    this.event('transport', { detail: `JEDEC ROM seule : ${raw === null ? 'non lu' : '0x' + raw.toString(16).padStart(8, '0')} · lecture ${read} · tentative ${this.identityChecks} · stub ${evidence.loader_stub ? 'oui' : 'non'}${valid ? '' : ' · capacité flash inconnue'}` });
    if (valid) return { chip, flash_size: 2 ** code, flash_id: raw, mac, flash_size_known: true };
    // flash_id stays null: a garbage JEDEC must not become a physical identifier that a reconnection would compare.
    return { chip, flash_size: null, flash_id: null, mac, flash_size_known: false, flash_capacity: evidence };
  }
  async reset() {
    if (this.native && !this.usbSerialJtag) throw new HardwareError('external_uart_required');
    if (this.mode !== 'console') throw new HardwareError('console_required');
    // 303a:1001 interprets CDC RTS as an internal core reset, not a physical EN pin.
    // Keep DTR deasserted: do not request download mode or expose an IO0 override.
    let failure;
    try {
      await this.port.setSignals({ dataTerminalReady: false, requestToSend: true });
      await sleep(this.usbSerialJtag ? 200 : 100);
    } catch (error) { failure = error; }
    finally {
      try { await this.port.setSignals({ dataTerminalReady: false, requestToSend: false }); }
      catch (error) { if (!failure) failure = error; }
    }
    if (failure) throw failure;
    if (this.usbSerialJtag) await sleep(200);
    return this.usbSerialJtag ? 'usb_serial_jtag' : 'en';
  }
  // Cooperative wait: each slice re-checks the lease / grant / deadline so a revocation cuts the hold short.
  async pause(ms, context) {
    const end = performance.now() + ms;
    for (;;) {
      context.check?.();
      const left = end - performance.now();
      if (left <= 0) return;
      await sleep(Math.min(left, 50));
    }
  }
  // ONE atomic sequence for the external UART bridge with the CLASSIC two-transistor auto-reset circuit (esptool ClassicReset).
  // Truth table (DTR, RTS asserted = 1) -> (EN, IO0):  (0,0)->(1,1)  (1,1)->(1,1)  (0,1)->(0,1)=EN low  (1,0)->(1,0)=IO0 low.
  // Asserting BOTH lines therefore does NOT reset the chip: the sequence must alternate them.
  //   1. DTR=0 RTS=1 : IO0 high, EN low (chip held in reset)
  //   2. DTR=1 RTS=0 : EN released while IO0 is low (the ROM samples the strapping pins at the EN rising edge); bounded hold
  //   3. DTR=0 RTS=0 : both lines released, on every path where the transport still answers.
  // The wiring is an ASSUMPTION about the bridge (same as reset/io0): a bridge without this circuit may behave differently.
  // Never retried. If the release itself fails the line state is unknown: operator recovery, not an idle card.
  async bootDownload(holdMs, context) {
    if (this.native) throw new HardwareError('external_uart_required');
    if (this.mode !== 'console') throw new HardwareError('console_required');
    let failure;
    try {
      await this.port.setSignals({ dataTerminalReady: false, requestToSend: true });
      await this.pause(100, context);
      await this.port.setSignals({ dataTerminalReady: true, requestToSend: false });
      await this.pause(holdMs, context);
    } catch (error) { failure = error; }
    try { await this.port.setSignals({ dataTerminalReady: false, requestToSend: false }); }
    catch (error) {
      this.setHold(true);
      throw new HardwareError('signal_release_failed');
    }
    if (failure) throw failure;
    return 'en+io0';
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
    const generation = this.triggerGeneration;
    this.triggerTimer = setTimeout(async () => {
      // A delayed trigger must never collide with flash, another reservation, or a disconnected server.
      if (generation !== this.triggerGeneration || !trigger.live() || this.hold || performance.now() >= trigger.until) return;
      const resetting = trigger.action === 'reset';
      if (resetting && (this.busy || this.mode !== 'console')) return;
      if (resetting) this.busy = true;
      try { if (trigger.action === 'reset') await this.reset(); else this.event('mark', { detail: 'trigger' }); }
      catch { this.event('transport', { detail: 'trigger action failed' }); }
      finally { if (resetting) this.busy = false; }
    }, trigger.delay);
  }
  async execute(command, args, context) {
    if (this.busy) throw new Error('Device is busy');
    if (this.romOnly && !['identify', 'mark', P4_TRACE, P4_BRINGUP, P4_FLASHER_READ].includes(command)) throw new HardwareError('diagnostic_rom_only_session');
    if (this.diagnosticResident && command !== 'mark') throw new HardwareError('operator_recovery_required');
    if (this.hold && !['identify', 'partitions', 'flash.read', 'flash.write', 'flash.erase', 'mark'].includes(command)) throw new Error('Operator recovery required');
    if (command === P4_TRACE) return this.diagnosticP4(args, context);
    if (command === P4_BRINGUP) return this.diagnosticBringup(args, context);
    if (command === P4_FLASHER_READ) return this.diagnosticBringup(args, context, P4_FLASHER_READ, runP4FlasherRead);
    this.busy = true; this.phase = 'prepare'; this.readProgress = {}; this.lastRead = {}; this.flashReadProfile = null;
    this.baselineTransport = this.transport; this.serialBaseline = { ...this.transport?.stats };
    let destructive = false;
    try {
      context.check(); this.setPhase('control', context);
      if (command === 'mark') { this.event('mark', { detail: args.label }); return { label: args.label }; }
      if (command === 'trigger') {
        if (args.action === 'disarm') { this.cancelTrigger(); return { armed: false }; }
        if (args.action === 'reset' && !this.capabilities().includes('reset')) throw new HardwareError('external_uart_required');
        this.cancelTrigger();
        this.trigger = { bytes: unb64(args.data), action: args.action, until: performance.now() + args.expires_ms,
          delay: args.delay_ms || 0, live: context.triggerLive || context.live };
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
      if (command === 'console') { this.setPhase('console', context); await this.openConsole(true); return { mode: this.mode }; }
      if (command === 'reset') { const method = await this.reset(); return { reset: true, method, boot_verified: false }; }
      if (command === 'boot.download') {
        const method = await this.bootDownload(args.hold_ms, context);
        return { download_mode_requested: true, method, hold_ms: args.hold_ms, boot_verified: false };
      }
      if (command === 'io0') {
        if (this.native) throw new HardwareError('external_uart_required');
        if (this.mode !== 'console') throw new HardwareError('console_required');
        await this.port.setSignals({ dataTerminalReady: true, requestToSend: false });
        try { await sleep(args.duration_ms); } finally { await this.port.setSignals({ dataTerminalReady: false, requestToSend: false }); }
        return { duration_ms: args.duration_ms };
      }
      if (command === 'uart.configure') {
        await this.closeConsole(); this.baudrate = args.baudrate;
        await this.openConsole(true); return { baudrate: this.baudrate };
      }
      if (command === 'uart.write') {
        if (this.mode !== 'console') throw new HardwareError('console_required');
        const writer = this.port.writable.getWriter();
        try { await writer.write(unb64(args.data)); } finally { writer.releaseLock(); }
        return { sent: unb64(args.data).length };
      }
      // Download and verify all firmware bytes before changing the boot state.
      const images = [];
      if (command === 'flash.write') {
        this.setPhase('artifact_download', context);
        for (const i of args.images) {
          const data = await context.download(i.artifact);
          if (await sha256(data) !== i.sha256) throw new HardwareError('artifact_integrity_failed');
          images.push({ ...i, data });
        }
      }
      context.check();
      this.setPhase('identify', context);
      const physical = await this.identity(), l = this.loader;
      if (command.startsWith('flash.') || command === 'partitions') {
        if (!Number.isInteger(physical.flash_size)) throw new HardwareError('flash_capacity_unknown', { phase: 'identify' });
        if (args.chip && args.chip !== physical.chip) throw new Error('Physical chip mismatch');
        if (physical.chip !== context.device.chip || physical.flash_size !== context.device.flash_size) throw new HardwareError('physical_identity_changed');
      }
      context.check();
      let result;
      if (command === 'identify') result = physical;
      else if (command === 'partitions') {
        region(args.address ?? 0x8000, 0xc00, physical.flash_size);
        this.setPhase('flash_read', context);
        const bytes = await this.readFlash(l, args.address ?? 0x8000, 0xc00, context);
        try { result = { partitions: partitions(bytes, physical.flash_size) }; if (!result.partitions.length) throw new Error('Empty table'); }
        catch { throw new HardwareError('partition_table_invalid'); }
      } else if (command === 'flash.read') {
        const reader = args.reader ?? 'reference';
        if (!['reference', 'preallocated', 'packet-diagnostic', 'md5-only'].includes(reader)) throw new HardwareError('flash_reader_invalid');
        const packetSize = reader === 'packet-diagnostic' ? args.packet_size : 4096;
        if (reader === 'packet-diagnostic' ? ![1024, 2048, 4096].includes(packetSize) : args.packet_size !== undefined)
          throw new HardwareError('flash_packet_size_invalid');
        region(args.address, args.size, physical.flash_size);
        this.setPhase('flash_read', context);
        if (reader === 'md5-only') {
          if (args.size > MD5_DIAGNOSTIC_MAX_SIZE) throw new HardwareError('flash_md5_diagnostic_size_exceeded');
          context.check();
          const start = performance.now();
          const md5 = await flashMd5Diagnostic(l, args.address, args.size);
          context.check();
          result = { address: args.address, size: args.size, md5,
            transfer: { bytes: args.size, elapsed_ms: Math.round(performance.now() - start),
              reader: 'md5-only', transmitted_flash_bytes: 0, packet_size: null,
              metric: 'flash_read_and_md5_without_bulk_transfer' } };
        } else {
          const data = await this.readFlash(l, args.address, args.size, context, reader, packetSize);
          this.setPhase('artifact_upload', context);
          result = { artifact: await context.upload(data), address: args.address, transfer: this.lastFlashTransfer };
        }
      } else if (command === 'flash.write') {
        validateImages(images, physical.flash_size); context.check();
        this.setPhase('flash_write', context);
        destructive = true; this.setHold(true);
        const writeStartedAt = performance.now();
        result = await runWrite(l, images.map(i => ({ data: i.data, addr: i.address, path: i.artifact, sha: i.sha256 })),
          { erase: args.erase === true, tracer: this.trace });
        if (result.ok) result = { ...result, transfer: flashTransfer(images.reduce((sum, image) => sum + image.data.length, 0), performance.now() - writeStartedAt) };
        if (!result.ok) return { ...result, hold: true };
        if (context.live() && !context.deferCompletion) this.setHold(false);
      } else if (command === 'flash.erase') {
        this.setPhase('flash_erase', context);
        context.check(); destructive = true; this.setHold(true);
        await l.eraseFlash(); result = { erased: true };
        if (context.live() && !context.deferCompletion) this.setHold(false);
      } else throw new Error('Unsupported command');
      if (context.deferCompletion) {
        const hold = destructive ? !context.live() : this.hold;
        context.complete({ hold, returnConsole: args.return_console !== false && !hold && context.live() });
      } else if (args.return_console !== false && !this.hold && context.live()) await this.openConsole();
      return result;
    } catch (e) {
      if (destructive) { this.setHold(true); return { state: 'indeterminate', hold: true, error: { code: 'hardware_outcome_unknown' }, trace: this.trace.snapshot().slice(-60) }; }
      // Capture the original phase and transport failure before cleanup closes
      // the port. Cleanup errors must not replace the useful diagnostic.
      const failure = hardwareError(e, this.disconnected, this.diagnostics());
      if (this.phase === 'flash_read') { try { await this.close(); } catch { /* Original failure wins. */ } }
      throw Object.assign(new HardwareError(failure.code, failure), { name: failure.error_type, kind: failure.transport_kind });
    } finally { this.busy = false; }
  }

  // RAM-only P4 USB bring-up. Reuses the existing ROM-only session and
  // conservative hold/recovery lifecycle of the legacy diagnostic.
  async diagnosticBringup(args, context, command = P4_BRINGUP, run = runP4UsbBringup) {
    if (this.busy) throw new Error('Device is busy');
    if (this.hold) throw new HardwareError('operator_recovery_required');
    if (!this.capabilities().includes(command)) throw new HardwareError('diagnostic_unavailable');
    this.busy = true; this.phase = 'prepare'; this.readProgress = {}; this.lastRead = {};
    let touched = false;
    try {
      context.check(); this.setPhase('artifact_download', context);
      const stubBytes = await context.download(args.stub_artifact);
      context.check(); this.setPhase('identify', context);
      const loader = await this.openLoader();
      this.baselineTransport = this.transport; this.serialBaseline = {...this.transport?.stats};
      this.setHold(true); this.diagnosticResident = true; touched = true;
      const result = await run({
        loader, args, device: context.device, stubBytes, romOnly: this.romOnly === true,
        check: () => context.check(), checkLive: () => (context.checkLive ?? context.check)(), report: detail => context.report?.(detail),
      });
      context.complete?.({hold: true, returnConsole: false});
      return result;
    } catch (error) {
      if (touched && error?.indeterminate === false) {
        this.diagnosticResident = false; this.setHold(false); touched = false;
      }
      const failure = hardwareError(error, this.disconnected, this.diagnostics());
      if (touched) {
        this.setHold(true); this.diagnosticResident = true;
        context.complete?.({hold: true, returnConsole: false});
        const {code: originCode, ...structured} = failure;
        return {state: 'indeterminate', hold: true,
          error: {code: 'hardware_outcome_unknown', origin_code: originCode, ...structured},
          trace: this.trace.snapshot().slice(-60)};
      }
      throw Object.assign(new HardwareError(failure.code, failure), {name: failure.error_type});
    } finally { this.busy = false; }
  }

  // Experimental P4 USB OTG diagnostic. Not a flash read: RAM code execution. Hold is raised BEFORE the first chip write;
  // an early refusal (nothing sent) clears it again, anything else stays held, indeterminate, with no retry and no reset.
  async diagnosticP4(args, context) {
    if (this.busy) throw new Error('Device is busy');
    if (this.hold) throw new HardwareError('operator_recovery_required');
    if (!this.capabilities().includes(P4_TRACE)) throw new HardwareError('diagnostic_unavailable');
    this.busy = true; this.phase = 'prepare'; this.readProgress = {}; this.lastRead = {}; this.flashReadProfile = null;
    let touched = false;
    try {
      context.check(); this.setPhase('artifact_download', context);
      const stubBytes = await context.download(args.stub_artifact);
      context.check(); this.setPhase('identify', context);
      const loader = await this.openLoader();   // the ROM-only connection itself
      this.baselineTransport = this.transport; this.serialBaseline = { ...this.transport?.stats };
      this.setHold(true); this.diagnosticResident = true; touched = true;
      this.setPhase('flash_read', context);
      const { result } = await runP4UsbTrace({ loader, args, device: context.device, stubBytes, romOnly: this.romOnly === true, check: () => context.check(),
        report: detail => context.report?.(detail) });
      context.complete?.({ hold: true, returnConsole: false });
      return result;
    } catch (e) {
      if (touched && e?.indeterminate === false) {
        // Refused before any byte reached the chip (or by the loader's own pre-checks): nothing resident, release the hold.
        this.diagnosticResident = false; this.setHold(false); touched = false;
      }
      // One structured, allowlisted report for every outcome: phase, origin code, category, RAM-load state (never loader text).
      const failure = hardwareError(e, this.disconnected, this.diagnostics());
      if (touched) {
        this.setHold(true); this.diagnosticResident = true;
        context.complete?.({ hold: true, returnConsole: false });
        const { code: originCode, ...structured } = failure;
        return { state: 'indeterminate', hold: true,
          error: { code: 'hardware_outcome_unknown', origin_code: originCode, ...structured },
          trace: this.trace.snapshot().slice(-60) };
      }
      throw Object.assign(new HardwareError(failure.code, failure), { name: failure.error_type });
    } finally { this.busy = false; }
  }
}

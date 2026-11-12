// Hardware only: no DOM, REST, project readiness rules or verdicts.
import { HardwareError, hardwareError } from './hardware-errors.js';
import { makeBenchTransport } from './bench-transport.js';
import { runWrite, Tracer } from './flash-op.js';
import { b64, unb64, family, region, validateImages, partitions, sha256 } from './protocol.js';
import { STUB_VARIANTS } from './stub-variants.js';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export class EspEngine {
  constructor(port, deps, event, hold = () => {}) {
    this.port = port; this.deps = deps; this.event = event; this.persistHold = hold;
    const usb = port.getInfo();
    this.native = usb.usbVendorId === 0x303a;
    this.usbSerialJtag = this.native && usb.usbProductId === 0x1001;
    this.mode = 'off'; this.reader = null; this.loader = null; this.loaderMode = null; this.loaderKey = null; this.loaderVariant = null; this.baudrate = 115200;
    this.flashBaud = 460800; this.identityChecks = 0; this.hold = false; this.trigger = null; this.tail = new Uint8Array();
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
      serial_timeouts: Math.max(0, (stats.timeouts || 0) - (base.timeouts || 0)), ...this.lastRead };
  }
  setPhase(phase, context) { this.phase = phase; context.report?.(this.diagnostics()); }
  // After the data of a READ_FLASH, the stub sends one more frame: the MD5 of the bytes it read from flash, computed before they were sent.
  // esptool-js 0.7.0 never reads it (the next command flushes the input). With `digests` given, read it here, no new command: the bytes
  // were already sent. The state is reported as observed: received, absent (timeout), partial, invalid. A closed port is NOT classified: it
  // is a disconnection and propagates.
  async trailingDigest(loader) {
    let frame;
    try { frame = await loader.transport.read(3000); }
    catch (e) {
      if (e?.kind === 'no-response') return { state: 'absent' };
      if (e?.kind === 'partial-packet') return { state: 'partial' };
      if (e?.kind === 'invalid-packet') return { state: 'invalid' };
      throw e;
    }
    if (!(frame instanceof Uint8Array)) return { state: 'invalid', length: 0 };
    if (frame.length !== 16) return { state: frame.length < 16 ? 'partial' : 'invalid', length: frame.length };
    return { state: 'received', md5: Array.from(frame, b => b.toString(16).padStart(2, '0')).join('') };
  }
  // MD5 of a flash range computed by the chip itself (SPI_FLASH_MD5, 0x13), through the existing esptool-js primitive. The answer is checked, never
  // defaulted: a stub answers 16 raw bytes (32 hex characters), the ROM 32 ASCII characters (esptool-js returns their hex, 64 characters).
  async chipMd5(loader, address, size) {
    const hex = await loader.flashMd5sum(address, size);
    if (loader.IS_STUB) {
      if (typeof hex === 'string' && /^[0-9a-f]{32}$/.test(hex)) return { md5: hex, format: 'stub-raw16' };
    } else if (typeof hex === 'string' && /^(?:3[0-9]|6[1-6]){32}$/.test(hex)) {
      return { md5: hex.match(/../g).map(h => String.fromCharCode(parseInt(h, 16))).join(''), format: 'rom-ascii32' };
    }
    throw new HardwareError('md5_response_invalid', { phase: 'flash_md5' });
  }
  async readFlash(loader, address, size, context, digests = null) {
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
      const block = await loader.readFlash(address + offset, blockSize, (_packet, received) => {
        check();
        if (Number.isSafeInteger(received) && received >= 0 && received <= blockSize) this.readProgress.received_bytes = offset + received;
        if (this.readProgress.received_bytes >= nextReport) {
          context.report?.(this.diagnostics()); nextReport = this.readProgress.received_bytes + 256 * 1024;
        }
      });
      if (!(block instanceof Uint8Array) || block.length !== blockSize) throw new HardwareError('short_flash_read');
      data.set(block, offset); this.readProgress.received_bytes = offset + blockSize;
      if (digests) {
        // After a digest that is not `received` the byte stream is no longer known to be aligned on frames (a late or truncated frame may still arrive):
        // the data keeps being read as before, but no further frame is read or interpreted. The remaining blocks are reported as not attempted.
        const broken = digests.some(d => d.state !== 'received');
        digests.push({ block_address: address + offset, block_size: blockSize, ...(broken ? { state: 'not_attempted' } : await this.trailingDigest(loader)) });
      }
      context.report?.(this.diagnostics()); check();
    }
    return data;
  }
  setHold(value) { this.hold = value; this.persistHold(value); }
  cancelTrigger() {
    clearTimeout(this.triggerTimer);
    this.triggerTimer = null; this.trigger = null; this.tail = new Uint8Array();
    this.triggerGeneration = (this.triggerGeneration || 0) + 1;
  }
  capabilities() {
    const caps = ['identify', 'partitions', 'flash.read', 'flash.md5', 'flash.write', 'flash.erase', 'uart.write', 'uart.configure', 'console', 'mark', 'trigger', 'http.request'];
    if (!this.native) return [...caps, 'reset', 'io0', 'boot.download'];
    return this.usbSerialJtag ? [...caps, 'reset'] : caps;
  }
  async openConsole() {
    if (this.hold) throw new HardwareError('operator_recovery_required');
    if (this.mode === 'console') return;
    this.event('transport', { detail: 'opening console' });
    if (this.transport) { await this.transport.disconnect(); this.transport = null; this.loader = null; this.loaderMode = null; this.loaderKey = null; this.loaderVariant = null; }
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
    if (this.transport) { await this.transport.disconnect(); this.transport = null; this.loader = null; this.loaderMode = null; this.loaderKey = null; this.loaderVariant = null; }
    // A failed/partial open may leave Web Serial open while the engine mode is off.
    if (this.port.readable || this.port.writable) await this.port.close();
    this.mode = 'off';
  }
  // ---- experimental stub variants (never selected by default) ----------------------------------------------------------------------------
  // Everything that can refuse does so BEFORE the port is touched: unknown name, wrong chip family declared by the device, a request that does not
  // carry the pinned SHA-256, a file that does not hash to it, a malformed JSON, or a session already open with another stub.
  async loadVariantFile(file) {
    if (this.deps.fetchAsset) return this.deps.fetchAsset(file);
    const response = await fetch(new URL(file, import.meta.url));
    if (!response.ok) throw new HardwareError('stub_variant_file_unavailable');
    return new Uint8Array(await response.arrayBuffer());
  }
  async prepareVariant(command, args, context) {
    const name = args.stub_variant;
    if (name === undefined && args.stub_sha256 === undefined) return null;
    const entry = Object.hasOwn(STUB_VARIANTS, name) ? STUB_VARIANTS[name] : null;
    if (!entry) throw new HardwareError('stub_variant_unknown');
    if (!entry.allowed_commands.includes(command)) throw new HardwareError('stub_variant_not_allowed');          // read only, whatever the server let through
    if (args.loader === 'rom') throw new HardwareError('stub_variant_not_allowed');
    if (typeof args.stub_sha256 !== 'string' || args.stub_sha256 !== entry.sha256) throw new HardwareError('stub_variant_sha_mismatch');
    const key = 'stub:' + name;
    if (this.loader && (this.loaderKey ?? 'stub') !== key) throw new HardwareError('loader_mode_mismatch', { phase: 'identify', loader_stub: this.loader.IS_STUB ? 1 : 0 });
    if (this.loader) return { name, entry, key, bytes: null };                       // same variant already loaded in this session: nothing to re-read
    const device = context?.device?.chip;
    if (device !== undefined && device !== family(entry.chip)) throw new HardwareError('stub_variant_chip_mismatch');
    const bytes = await this.loadVariantFile(entry.file);
    if (await sha256(bytes) !== entry.sha256) throw new HardwareError('stub_variant_sha_mismatch');
    let stub;
    try { stub = JSON.parse(new TextDecoder().decode(bytes)); } catch { throw new HardwareError('stub_variant_sha_mismatch'); }
    const fields = ['entry', 'text', 'text_start', 'data', 'data_start', 'bss_start'];
    if (!fields.every(k => Object.hasOwn(stub, k)) || typeof stub.text !== 'string' || typeof stub.data !== 'string' || await sha256(new TextEncoder().encode(stub.text)) !== entry.text_sha256) {
      throw new HardwareError('stub_variant_sha_mismatch');
    }
    const decoded = { text: unb64(stub.text), data: unb64(stub.data) };
    if (decoded.text.length !== entry.decoded_text_bytes || decoded.data.length !== entry.decoded_data_bytes) throw new HardwareError('stub_variant_sha_mismatch');
    return { name, entry, key, stub, decoded };
  }
  // Same sequence as ESPLoader.runStub of esptool-js 0.7.0 (memBegin/memBlock for text then data, memFinish, wait for "OHAI", IS_STUB = true),
  // with the variant's bytes. Runs inside main() in place of runStub, i.e. after chip detection and the ECO7 flash power sequence.
  async uploadVariant(loader, variant) {
    if (loader.syncStubDetected || loader.IS_STUB) throw new HardwareError('stub_variant_stub_running', { phase: 'identify', loader_stub: 1 });
    const name = loader.chip?.CHIP_NAME, revision = loader.chip?.getChipRevision ? await loader.chip.getChipRevision(loader) : undefined;
    if (name !== variant.entry.chip || !(revision >= variant.entry.minRevision)) throw new HardwareError('stub_variant_chip_mismatch', { phase: 'identify', loader_stub: 0 });
    const { stub, decoded } = variant;
    for (const [bytes, address] of [[decoded.text, stub.text_start], [decoded.data, stub.data_start]]) {
      if (!bytes.length) continue;
      const blocks = Math.floor((bytes.length + loader.ESP_RAM_BLOCK - 1) / loader.ESP_RAM_BLOCK);
      await loader.memBegin(bytes.length, blocks, loader.ESP_RAM_BLOCK, address);
      for (let sequence = 0; sequence < blocks; sequence++) {
        const start = sequence * loader.ESP_RAM_BLOCK;
        await loader.memBlock(bytes.slice(start, start + loader.ESP_RAM_BLOCK), sequence);
      }
    }
    await loader.memFinish(stub.entry);
    let greeting;
    try { greeting = await loader.transport.read(loader.DEFAULT_TIMEOUT); }
    catch (e) { if (e?.kind === 'closed') throw e; throw new HardwareError('stub_variant_start_failed', { phase: 'identify', loader_stub: 0 }); }   // silence, truncated or noisy: the stub did not start
    if (String.fromCharCode(...greeting) !== 'OHAI') throw new HardwareError('stub_variant_start_failed', { phase: 'identify', loader_stub: 0 });
    loader.IS_STUB = true;
    await loader.applyUsbFlashWriteSize();
    return loader.chip;
  }
  // What the evidence says about the stub that actually ran: the official one is the bundled stub (its bytes are not visible to the agent), a variant is
  // identified by the hashes it was verified against.
  stubIdentity() {
    const variant = this.loaderVariant;
    if (!variant) return { variant: 'official', origin: 'esptool-js bundled stub', verified: false, uart_baud: this.flashBaud };
    return { variant: variant.name, origin: 'experimental, uploaded by the agent', verified: true, file_sha256: variant.entry.sha256, text_sha256: variant.entry.text_sha256,
      uart_baud: variant.entry.uart_baud, stub: variant.entry.origin.stub, stub_commit: variant.entry.origin.stub_commit, lib_commit: variant.entry.origin.lib_commit,
      patch_sha256: variant.entry.origin.patch_sha256 };
  }
  // `mode` 'stub' (default, unchanged) or 'rom': esptool-js main() always uploads the stub, so in 'rom' mode its runStub step is replaced, on this
  // loader instance only, by a no-op. Nothing else is bypassed: the chip is detected, the ECO7 flash power sequence (postConnect) still runs, the baud rate
  // stays the ROM one, and the flash is attached with the same 8-byte SPI_ATTACH payload as esptool (esptool-js flashSpiAttach sends only 4).
  // A session that is already open in the other mode is refused, never reused silently.
  async openLoader(mode = 'stub', variant = null) {
    if (this.loader) {
      if ((this.loaderKey ?? this.loaderMode ?? 'stub') !== (variant ? variant.key : mode)) throw new HardwareError('loader_mode_mismatch', { phase: 'identify', loader_stub: this.loader.IS_STUB ? 1 : 0 });
      return this.loader;
    }
    await this.closeConsole();
    this.event('transport', { detail: mode === 'rom' ? 'opening loader (ROM, no stub)' : 'opening loader' });
    const { ESPLoader, Transport, ESPError } = this.deps;
    const { BenchTransport } = makeBenchTransport(Transport, ESPError);
    this.transport = new BenchTransport(this.port, false);
    this.transport.onEvent = (name, detail) => {
      this.trace.ev(name, detail); this.lastTransportStats = this.transport?.stats;
      if (name === 'read-start') this.lastRead = { silence_limit_ms: detail.timeout };
      if (name === 'read-end') this.lastRead = { ...this.lastRead, last_read_ms: detail.ms, last_packet_bytes: detail.bytes };
    };
    const terminal = { clean() {}, write() {}, writeLine: () => {} };
    const rom = mode === 'rom';
    this.loader = new ESPLoader({ transport: this.transport, baudrate: rom ? 115200 : variant ? variant.entry.uart_baud : this.flashBaud, terminal });
    if (variant) { const engine = this; this.loader.runStub = async function () { return engine.uploadVariant(this, variant); }; }
    // esptool-js main() order: detect chip, postConnect (ECO7 flash power sequence), runStub, [baud change], readFlashId. In ROM mode runStub is replaced, on this loader
    // instance only, by the SPI attach (esptool's own order: power on, attach, then any flash id read). It reproduces the branches esptool-js already has for "no stub
    // available" (return this.chip, IS_STUB untouched). The only things the real runStub does besides the upload (IS_STUB = true, applyUsbFlashWriteSize for USB-OTG writes)
    // are not needed for an MD5.
    if (rom) {
      this.loader.runStub = async function () {
        // A stub already running on the chip is detected from the chip's own sync answers (syncStubDetected): refuse before any flash command.
        if (this.syncStubDetected || this.IS_STUB) throw new HardwareError('rom_mode_stub_running', { phase: 'identify', loader_stub: 1 });
        // 8-byte SPI_ATTACH payload of esptool Python (hspi_arg, then the reserved "legacy" word); esptool-js flashSpiAttach sends only 4.
        try { await this.checkCommand('configure SPI flash pins', this.ESP_SPI_ATTACH, new Uint8Array(8)); }
        catch (e) { throw new HardwareError('rom_flash_attach_failed', { phase: 'identify', loader_stub: 0 }); }
        return this.chip;
      };
    }
    this.identityChecks = 0;   // identity re-checks made on THIS loader session (diagnostic only)
    try {
      this.chipDescription = await this.loader.main(); this.mode = 'loader'; this.loaderMode = mode; this.loaderKey = variant ? variant.key : mode; this.loaderVariant = variant;
      if (rom && (this.loader.syncStubDetected || this.loader.IS_STUB)) throw new HardwareError('rom_mode_stub_running', { phase: 'identify', loader_stub: 1 });
    }
    catch (e) { try { await this.close(); } catch { /* Preserve the original serial failure. */ } throw e; }
    return this.loader;
  }
  async identity(mode = 'stub', variant = null) {
    const l = await this.openLoader(mode, variant);
    this.identityChecks += 1;
    const id = await l.readFlashId();
    const code = id >>> 16 & 255;
    // The check itself is unchanged. Only its failure now carries the raw JEDEC value read, the rank of this check in the loader
    // session and whether the stub runs, so that a second identification that fails is explained by evidence and not by guesswork.
    if (code < 12 || code > 26) throw new HardwareError('flash_capacity_unknown', { phase: 'identify', jedec_id: id >>> 0, identify_index: this.identityChecks, loader_stub: l.IS_STUB ? 1 : 0 });
    const chip = family(this.chipDescription.replace(/\s*\(.*$/, ''));
    return { chip, flash_size: 2 ** code, flash_id: id, mac: await l.chip.readMac(l) };
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
    if (this.hold && !['identify', 'partitions', 'flash.read', 'flash.write', 'flash.erase', 'mark'].includes(command)) throw new Error('Operator recovery required');
    this.busy = true; this.phase = 'prepare'; this.readProgress = {}; this.lastRead = {};
    this.baselineTransport = this.transport; this.serialBaseline = { ...this.transport?.stats };
    let destructive = false;
    try {
      context.check(); this.setPhase('control', context);
      // Defense in depth: stub-variant arguments only ever reach the read commands the registry allows, whatever the server let through.
      if ((args.stub_variant !== undefined || args.stub_sha256 !== undefined) && !['flash.md5', 'flash.read'].includes(command)) throw new HardwareError('stub_variant_not_allowed');
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
      if (command === 'console') { this.setPhase('console', context); await this.openConsole(); return { mode: this.mode }; }
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
        await this.openConsole(); return { baudrate: this.baudrate };
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
      const variant = await this.prepareVariant(command, args, context);       // refuses BEFORE any serial access; null unless a variant was asked for
      const physical = await this.identity(command === 'flash.md5' && args.loader === 'rom' ? 'rom' : 'stub', variant), l = this.loader;
      // Diagnostic only: the raw JEDEC id that THIS command's identification read, its rank in the loader session and whether the stub runs.
      const identified = { jedec_id: physical.flash_id >>> 0, identify_index: this.identityChecks, loader_stub: l.IS_STUB ? 1 : 0,
        chip_description: String(this.chipDescription || '').slice(0, 64) };
      if (command.startsWith('flash.') || command === 'partitions') {
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
        try { result = { partitions: partitions(bytes, physical.flash_size), identify: identified }; if (!result.partitions.length) throw new Error('Empty table'); }
        catch { throw new HardwareError('partition_table_invalid'); }
      } else if (command === 'flash.read') {
        region(args.address, args.size, physical.flash_size);
        this.setPhase('flash_read', context);
        const digests = args.stub_digest === true ? [] : null;
        const data = await this.readFlash(l, args.address, args.size, context, digests);
        this.setPhase('artifact_upload', context);
        result = { artifact: await context.upload(data), address: args.address, identify: identified, stub_identity: this.stubIdentity(), ...(digests ? { stub_digests: digests } : {}) };
      } else if (command === 'flash.md5') {
        region(args.address, args.size, physical.flash_size);
        this.setPhase('flash_md5', context);
        result = { ...(await this.chipMd5(l, args.address, args.size)), address: args.address, size: args.size, loader: l.IS_STUB ? 'stub' : 'rom',
          // What the host actually sent: memory-load commands (ESP_MEM_BEGIN 5, END 6, DATA 7) are how a stub gets uploaded; 0 in a ROM session.
          ram_load_ops: ['5', '6', '7'].reduce((n, op) => n + ((this.transport?.commandCounts || {})[op] || 0), 0), stub_identity: this.stubIdentity(), identify: identified };
      } else if (command === 'flash.write') {
        validateImages(images, physical.flash_size); context.check();
        this.setPhase('flash_write', context);
        destructive = true; this.setHold(true);
        result = await runWrite(l, images.map(i => ({ data: i.data, addr: i.address, path: i.artifact, sha: i.sha256 })),
          { erase: args.erase === true, tracer: this.trace });
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
}

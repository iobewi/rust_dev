import { HardwareError, hardwareError } from './hardware-errors.js';
// Authenticated WebSocket executor; it owns no UI and never replays a hardware operation.
export class Agent {
  constructor({ engine, device, token, onState = () => {}, onLog = () => {}, onActivity = () => {}, socketFactory = url => new WebSocket(url), now = () => performance.now() }) {
    Object.assign(this, { engine, device, token, onState, onLog, onActivity, socketFactory, now });
    this.seq = 0; this.seen = new Set(); this.grantUntil = 0; this.pending = null; this.revoked = new Set(); this.awaitingResult = null;
  }
  log(stage, detail = {}) { this.onLog({ at: Date.now(), device: this.device.id, stage, ...detail }); }
  activity() { this.onActivity(Boolean(this.active || this.awaitingResult)); }
  releaseControl() {
    if (!this.generation || this.active || this.awaitingResult || this.engine.busy) throw new Error('Attendez la fin de l’opération');
    this.cancelTrigger(); this.grant(0); this.event('release_control', {});
  }
  connect() {
    this.log('connection');
    const url = new URL('/agent/v1/ws', location.href); url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.socket = this.socketFactory(url);
    this.socket.onopen = () => { this.authSent = this.now(); this.send({ type: 'auth', version: 1, ...(this.token ? { token: this.token } : {}) }); };
    this.socket.onmessage = e => this.message(JSON.parse(e.data)).catch(() => this.onState('agent error'));
    this.socket.onclose = () => {
      this.generation = null; this.grantUntil = 0; this.cancelTrigger();
      if (this.active || this.awaitingResult) this.engine.setHold(true);
      this.log('disconnected'); this.activity();
      this.onState('Agent disconnected; reconnect explicitly; no command is replayed');
    };
  }
  send(message) {
    if (this.socket?.readyState !== 1) return false;
    this.socket.send(JSON.stringify(message)); return true;
  }
  event(type, payload) {
    const message = { type, device: this.device.id, generation: this.generation, ...payload };
    if (type === 'uart') message.seq = ++this.seq;
    // Bound queued UART memory. Sequence numbers make every dropped chunk observable.
    if (!this.generation || this.socket?.bufferedAmount > 1024 * 1024) return;
    this.send(message);
  }
  grant(seconds) {
    this.log('grant', { seconds });
    this.grantUntil = this.now() + seconds * 1000;
    this.event('grant', { seconds });
  }
  cancelTrigger() {
    if (this.engine.cancelTrigger) this.engine.cancelTrigger();
    else this.engine.trigger = null;
  }
  recover() {
    if (this.active || this.awaitingResult) throw new Error('Operation is still running');
    this.recoveryPending = true; this.event('recover', {});
  }
  async artifact(id, bytes) {
    const { sha256 } = await import('./protocol.js');
    const phase = id ? 'artifact_download' : 'artifact_upload';
    try {
      const response = await fetch('/api/v1/artifacts' + (id ? '/' + id : ''), {
        method: id ? 'GET' : 'POST', headers: { ...(this.token ? { Authorization: 'Bearer ' + this.token } : {}),
          ...(bytes ? { 'X-SHA256': await sha256(bytes) } : {}) }, ...(bytes ? { body: bytes } : {}),
      });
      if (!response.ok) throw new HardwareError('artifact_transfer_failed', { phase, http_status: response.status });
      if (id) { const data = new Uint8Array(await response.arrayBuffer()); this.log('download', { bytes: data.length }); return data; }
      const result = await response.json(); this.log('upload', { bytes: bytes.length }); return result;
    } catch (error) {
      this.log('transfer_failed');
      if (error instanceof HardwareError) throw error;
      const failure = hardwareError(error, false, { phase });
      throw Object.assign(new HardwareError(failure.code, failure), { name: failure.error_type });
    }
  }

  async message(m) {
    if (m.type === 'welcome') {
      this.serverOffset = m.server_ms - this.authSent;
      this.send({ type: 'register', device: { ...this.device, hold: this.engine.hold } });
    } else if (m.type === 'registered') {
      this.generation = m.generation; this.seq = 0; this.log('connected'); this.onState('Agent connected');
    } else if (m.type === 'offer') {
      if (this.awaitingResult) { this.deferredOffer = m; return; }
      if (this.active || this.seen.has(m.id) || m.generation !== this.generation) return;
      // Remember offer deadline on a monotonic local clock; background throttling cannot extend it.
      this.pending = { ...m, until: m.deadline_ms - this.serverOffset - 100, received: this.now() };
      this.send({ type: 'ready', id: m.id });
    } else if (m.type === 'execute') {
      const op = this.pending; this.pending = null;
      if (!op || op.id !== m.id || this.seen.has(op.id) || this.active) return;
      this.seen.add(op.id);
      const until = Math.min(op.until, m.deadline_ms - this.serverOffset - 100);
      const executionUntil = m.execution_deadline_ms - this.serverOffset - 100;
      if (this.now() >= until) {
        const generation = this.generation, requiredHold = this.engine.hold;
        this.awaitingResult = { id: op.id, generation, requiredHold, returnConsole: false, live: () => this.generation === generation };
        this.engine.setHold(true); this.activity();
        this.send({ type: 'result', id: op.id, state: 'cancelled', error: { code: 'expired_before_execution' } }); return;
      }
      this.active = op; this.log('execute', { id: op.id, command: op.command }); this.activity();
      const started = this.now();
      // Persist before touching the port; a page crash must require operator review.
      this.engine.persistHold(true);
      const generation = this.generation;
      const privileged = ['flash.write', 'flash.erase', 'uart.write', 'io0', 'trigger', 'http.request'].includes(op.command) && !op.local_control;
      const leaseLive = () => this.generation === generation && this.socket?.readyState === 1 && !this.revoked.has(op.lease) && this.now() < op.lease_deadline_ms - this.serverOffset - 100 && (!privileged || this.now() < this.grantUntil);
      // The server starts this deadline before sending execute; network delay cannot extend it.
      const live = () => leaseLive() && this.now() < executionUntil;
      const timing = () => ({ elapsed_ms: this.now() - started,
        start_remaining_ms: Math.max(0, until - this.now()), execution_remaining_ms: Math.max(0, executionUntil - this.now()),
        lease_remaining_ms: Math.max(0, op.lease_deadline_ms - this.serverOffset - 100 - this.now()),
        ...(privileged ? { grant_remaining_ms: Math.max(0, this.grantUntil - this.now()) } : {}),
        visibility: typeof document === 'undefined' ? 'unavailable' : document.visibilityState });
      const checkLive = () => {
        if (this.generation !== generation || this.socket?.readyState !== 1) throw new HardwareError('agent_disconnected');
        if (this.revoked.has(op.lease) || this.now() >= op.lease_deadline_ms - this.serverOffset - 100) throw new HardwareError('reservation_expired');
        if (privileged && this.now() >= this.grantUntil) throw new HardwareError('grant_expired');
        if (this.now() >= executionUntil) throw new HardwareError('execution_deadline_exceeded');
      };
      let completion;
      const publish = message => {
        const requiredHold = completion ? completion.hold || !live() : this.engine.hold || !live();
        this.awaitingResult = { id: op.id, generation, requiredHold,
          returnConsole: completion?.returnConsole === true, live };
        this.engine.setHold(true);
        this.log('result', { id: op.id, command: op.command, state: message.state, code: message.error?.code, duration_ms: Math.round(this.now() - started), detail: message.error });
        this.send({ type: 'result', id: op.id, ...message, hold: requiredHold });
      };
      try {
        const check = () => {
          checkLive();
          if (this.now() >= until) throw new HardwareError('command_start_expired');
        };
        check();
        const result = await this.engine.execute(op.command, op.args, {
          check, checkLive, report: detail => this.log('progress', { id: op.id, command: op.command, detail }), live, triggerLive: leaseLive, deferCompletion: true,
          complete: value => { completion = value; }, device: this.device, download: id => this.artifact(id), upload: bytes => this.artifact(null, bytes),
        });
        if (!live()) this.engine.setHold(true);
        publish({ state: result?.state || 'completed', result, ...(result?.error ? { error: typeof result.error === 'object' ? result.error : { code: result.error_kind || 'hardware_command_failed' } } : {}) });
      } catch (error) {
        publish({ state: 'failed', error: hardwareError(error, this.engine.disconnected, { ...this.engine.diagnostics?.(), ...timing() }) });
      } finally {
        this.active = null; this.activity();
        this.engine.persistHold(this.awaitingResult !== null || this.engine.hold);
      }
    } else if (m.type === 'result_ack') {
      const pending = this.awaitingResult;
      if (!pending || pending.finalizing || pending.id !== m.id || m.generation !== pending.generation || m.generation !== this.generation) return;
      pending.finalizing = true;
      const hold = pending.requiredHold || m.quarantined !== false || !pending.live();
      try {
        this.engine.setHold(hold);
        if (!hold && pending.returnConsole) await this.engine.openConsole();
      } catch {
        this.engine.setHold(true); this.event('hold', {});
        this.onState('Console return failed; operator recovery required');
      } finally {
        this.awaitingResult = null; this.send({ type: 'settled', device: this.device.id, generation: this.generation, id: m.id }); this.log('acknowledged', { id: m.id, state: m.state }); this.activity();
        const offer = this.deferredOffer; this.deferredOffer = null;
        if (offer && this.generation) await this.message(offer);
      }
    } else if (m.type === 'control_released') {
      if (m.generation === this.generation) { this.log('control_released'); this.onState('Pilotage libéré par l’opérateur'); }
    } else if (m.type === 'recovered') {
      if (this.recoveryPending && m.generation === this.generation && !this.active) {
        this.engine.setHold(false); this.recoveryPending = false; this.onState('Operator recovery accepted');
      }
    } else if (m.type === 'lease_revoked') {
      this.revoked.add(m.lease); this.cancelTrigger();
    } else if (m.type === 'cancel') {
      // esptool-js cannot prove interruption. A request is observed; execution runs to an actual outcome.
      this.onState('Cancellation requested; hardware interruption is not confirmed');
    } else if (m.type === 'error') {
      if (this.awaitingResult?.id === m.id) { this.engine.setHold(true); this.socket?.close?.(); }
      this.log('protocol_error', { code: m.error.code });
      this.onState('Protocol error: ' + m.error.code);
    }
  }
}

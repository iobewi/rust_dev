// Authenticated WebSocket executor; it owns no UI and never replays a hardware operation.
export class Agent {
  constructor({ engine, device, token, onState = () => {}, socketFactory = url => new WebSocket(url) }) {
    Object.assign(this, { engine, device, token, onState, socketFactory });
    this.seq = 0; this.seen = new Set(); this.grantUntil = 0; this.pending = null; this.revoked = new Set();
  }
  connect() {
    const url = new URL('/agent/v1/ws', location.href); url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.socket = this.socketFactory(url);
    this.socket.onopen = () => { this.authSent = performance.now(); this.send({ type: 'auth', version: 1, token: this.token }); };
    this.socket.onmessage = e => this.message(JSON.parse(e.data)).catch(() => this.onState('agent error'));
    this.socket.onclose = () => {
      this.generation = null; this.grantUntil = 0; this.engine.trigger = null;
      if (this.active) this.engine.setHold(true);
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
    this.grantUntil = performance.now() + seconds * 1000;
    this.event('grant', { seconds });
  }
  recover() {
    if (this.active) throw new Error('Operation is still running');
    this.engine.setHold(false); this.event('recover', {});
  }
  async artifact(id, bytes) {
    const { sha256 } = await import('./protocol.js');
    const response = await fetch('/api/v1/artifacts' + (id ? '/' + id : ''), {
      method: id ? 'GET' : 'POST', headers: { Authorization: 'Bearer ' + this.token,
        ...(bytes ? { 'X-SHA256': await sha256(bytes) } : {}) }, ...(bytes ? { body: bytes } : {}),
    });
    if (!response.ok) throw new Error('Artifact transfer failed');
    return id ? new Uint8Array(await response.arrayBuffer()) : response.json();
  }
  async message(m) {
    if (m.type === 'welcome') {
      this.serverOffset = m.server_ms - this.authSent;
      this.send({ type: 'register', device: { ...this.device, hold: this.engine.hold } });
    } else if (m.type === 'registered') {
      this.generation = m.generation; this.seq = 0; this.onState('Agent connected');
    } else if (m.type === 'offer') {
      if (this.active || this.seen.has(m.id) || m.generation !== this.generation) return;
      // Remember offer deadline on a monotonic local clock; background throttling cannot extend it.
      this.pending = { ...m, until: m.deadline_ms - this.serverOffset - 100, received: performance.now() };
      this.send({ type: 'ready', id: m.id });
    } else if (m.type === 'execute') {
      const op = this.pending; this.pending = null;
      if (!op || op.id !== m.id || this.seen.has(op.id) || this.active) return;
      this.seen.add(op.id);
      const until = Math.min(op.until, m.deadline_ms - this.serverOffset - 100);
      if (performance.now() >= until) {
        this.send({ type: 'result', id: op.id, state: 'cancelled', error: { code: 'expired_before_execution' } }); return;
      }
      this.active = op;
      // Persist before touching the port; a page crash must require operator review.
      
      this.engine.persistHold(true);
      const generation = this.generation;
      const privileged = ['flash.write', 'flash.erase', 'uart.write', 'io0', 'trigger', 'http.request'].includes(op.command);
      const live = () => this.generation === generation && this.socket?.readyState === 1 && !this.revoked.has(op.lease) && performance.now() < op.lease_deadline_ms - this.serverOffset - 100 && (!privileged || performance.now() < this.grantUntil);
      try {
        const check = () => {
          if (!live() || performance.now() >= until) throw new Error('Expired command, lost connection or local grant');
        };
        check();
        const result = await this.engine.execute(op.command, op.args, {
          check, live, device: this.device, download: id => this.artifact(id), upload: bytes => this.artifact(null, bytes),
        });
        if (!live()) this.engine.setHold(true);
        this.send({ type: 'result', id: op.id, state: result?.state || 'completed', result,
          hold: this.engine.hold, ...(result?.error ? { error: result.error } : {}) });
      } catch {
        this.send({ type: 'result', id: op.id, state: 'failed', error: { code: 'hardware_command_failed' }, hold: this.engine.hold });
      } finally {
        this.active = null;
        this.engine.persistHold(this.engine.hold);
      }
    } else if (m.type === 'lease_revoked') {
      this.revoked.add(m.lease); this.engine.trigger = null;
    } else if (m.type === 'cancel') {
      // esptool-js cannot prove interruption. A request is observed; execution runs to an actual outcome.
      this.onState('Cancellation requested; hardware interruption is not confirmed');
    } else if (m.type === 'error') {
      this.onState('Protocol error: ' + m.error.code);
    }
  }
}

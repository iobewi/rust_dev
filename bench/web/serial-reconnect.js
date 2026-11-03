// Retry only the exact previously selected SerialPort, never a VID/PID match.
export class SerialReconnect {
  constructor({ ports, attach, idle, state = () => {}, schedule = setTimeout, cancel = clearTimeout }) {
    Object.assign(this, { ports, attach, idle, state, schedule, cancel }); this.version = 0;
  }
  remember(port, identity) { this.stop(); this.port = port; this.identity = identity; }
  stop() { this.version++; this.cancel(this.timer); this.waiting = false; }
  disconnected(port) {
    if (port !== this.port) return;
    this.stop(); this.waiting = true; this.retry(this.version);
  }
  retry(version) {
    this.timer = this.schedule(() => this.attempt(version), 1500);
  }
  async attempt(version = this.version) {
    if (!this.waiting || version !== this.version || this.running) return;
    this.running = true;
    try {
      if (!this.idle() || !(await this.ports()).includes(this.port)) return;
      if (version !== this.version) return;
      this.state('reconnecting');
      await this.attach(this.port, this.identity);
      if (version === this.version) { this.waiting = false; this.state('usb_connected'); }
    } catch (error) {
      if (error?.code === 'physical_identity_changed') { this.waiting = false; this.state('identity_changed'); }
      else this.state('reconnect_failed');
    } finally {
      this.running = false;
      if (this.waiting && version === this.version) this.retry(version);
    }
  }
}

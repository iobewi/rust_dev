// Per-board ownership boundary for one browser window. UI selection is presentation only.
// Never infer hardware ownership from VID/PID or the currently highlighted board.
export class BoardContexts {
  constructor({ allowed = [] } = {}) {
    this.allowed = new Set(allowed);
    this.boards = new Map();
    this.ports = new Map();
    this.selectedId = null;
  }

  setAllowed(ids) {
    this.allowed = new Set(ids);
    // Revoked IDs remain visible; further attachment is refused.
  }

  get(id = this.selectedId) { return this.boards.get(id) || null; }
  list() { return [...this.boards.values()]; }

  add(id, port, create) {
    if (!this.allowed.has(id)) throw new Error('device_forbidden');
    if (!port || typeof port !== 'object') throw new Error('invalid_serial_port');
    if (this.boards.has(id)) throw new Error('device_already_added');
    if (this.ports.has(port)) throw new Error('serial_port_in_use');
    // Reserve before creating potentially asynchronous resources. On failure,
    // free only this reservation, without disturbing any other board.
    const context = { id, port, status: 'connecting', identity: null,
      engine: null, agent: null, reconnect: null, journal: null, console: null,
      busy: false, disconnecting: false, generation: null };
    this.boards.set(id, context);
    this.ports.set(port, id);
    if (this.selectedId === null) this.selectedId = id;
    try {
      if (create) Object.assign(context, create(context));
      return context;
    } catch (error) {
      this.boards.delete(id); this.ports.delete(port);
      if (this.selectedId === id) this.selectedId = this.boards.keys().next().value || null;
      throw error;
    }
  }

  select(id) {
    if (!this.boards.has(id)) throw new Error('unknown_board');
    this.selectedId = id;
    return this.boards.get(id);
  }

  // All async operations retain their original context and generation.
  target(id = this.selectedId) {
    const ctx = this.get(id);
    if (!ctx || ctx.status !== 'connected' || !ctx.generation) throw new Error('device_unavailable');
    return Object.freeze({ device: ctx.id, generation: ctx.generation, context: ctx });
  }

  markConnected(id, identity, generation) {
    const ctx = this.require(id);
    ctx.identity = identity;
    ctx.generation = generation;
    ctx.status = 'connected';
    return ctx;
  }

  lost(port) {
    const id = this.ports.get(port);
    if (!id) return null;
    const ctx = this.require(id);
    if (ctx.status === 'disconnected' || ctx.disconnecting) return null;
    ctx.status = 'usb_lost';
    ctx.generation = null;
    return ctx;
  }

  disconnected(id) {
    const ctx = this.require(id);
    ctx.status = 'disconnected';
    ctx.generation = null;
    return ctx;
  }

  require(id) {
    const ctx = this.boards.get(id);
    if (!ctx) throw new Error('unknown_board');
    return ctx;
  }

  // Explicitly forgetting a board is prohibited while its resources are active.
  forget(id) {
    const ctx = this.require(id);
    if (ctx.status !== 'disconnected' || ctx.busy ||
        ctx.agent?.active || ctx.agent?.awaitingResult || ctx.engine?.busy ||
        ctx.reconnect?.running || ctx.reconnect?.waiting) throw new Error('board_not_idle');
    ctx.reconnect?.forget();
    this.boards.delete(id);
    this.ports.delete(ctx.port);
    if (this.selectedId === id) this.selectedId = this.boards.keys().next().value || null;
    return ctx;
  }
}

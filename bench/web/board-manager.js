import { BoardContexts } from './board-contexts.js';
import { SerialReconnect } from './serial-reconnect.js';

// No DOM dependency. One controller per browser window; exactly one physical
// SerialPort and one WebSocket agent per logical board. Selection is view-only.
export class BoardManager {
  constructor({ allowed, ports, createEngine, createAgent, onEvent = () => {},
                onState = () => {}, makeReconnect = options => new SerialReconnect(options),
                makeDevice = (ctx, identity, hardware) => ({
                  id: ctx.id, ...identity, capabilities: hardware.capabilities(),
                  connected: true, localOnly: true,
                }) }) {
    this.contexts = new BoardContexts({ allowed });
    this.ports = ports;
    this.createEngine = createEngine;
    this.createAgent = createAgent;
    this.onEvent = onEvent;
    this.onState = onState;
    this.makeReconnect = makeReconnect;
    this.makeDevice = makeDevice;
  }

  select(id) {
    const context = this.contexts.select(id);
    this.onState(context);
    return context;
  }

  get(id) { return this.contexts.get(id); }
  list() { return this.contexts.list(); }

  async add(id, port, options = {}) {
    const ctx = this.contexts.add(id, port);
    ctx.options = options;
    ctx.reconnect = this.makeReconnect({
      ports: () => this.ports(),
      idle: () => !ctx.busy && !ctx.disconnecting && !ctx.engine?.busy &&
        !ctx.agent?.active && !ctx.agent?.awaitingResult,
      attach: (exactPort, identity) => this.attach(ctx, exactPort, identity),
      state: stage => {
        if (stage === 'reconnecting') ctx.status = 'reconnecting';
        if (stage === 'identity_changed') ctx.status = 'usb_lost';
        this.onEvent(ctx, 'reconnect', { stage });
        this.onState(ctx);
      },
    });
    try {
      await this.attach(ctx, port);
      return ctx;
    } catch (error) {
      // An initial attach failure is not a previously connected board.
      // No other port or context is touched.
      ctx.reconnect.forget();
      this.contexts.disconnected(id);
      this.onState(ctx);
      throw error;
    }
  }

  async attach(ctx, port, expectedIdentity = null) {
    if (ctx.port !== port || this.contexts.ports.get(port) !== ctx.id) throw new Error('serial_port_mismatch');
    if (ctx.busy || ctx.disconnecting || ctx.engine?.busy || ctx.agent?.active ||
        ctx.agent?.awaitingResult) throw new Error('board_not_idle');
    if (ctx.agent) {
      ctx.agent.disconnect();
      ctx.agent = null;
    }
    if (ctx.engine) {
      await ctx.engine.close();
      ctx.engine = null;
    }
    const hardware = this.createEngine(ctx, port, (type, payload) => {
      // Capture ctx, never refer to the active selection here.
      ctx.agent?.event(type, payload);
      this.onEvent(ctx, type, payload);
    });
    ctx.engine = hardware;
    try {
      const identity = await hardware.identity();
      if (expectedIdentity && ['chip', 'flash_size', 'flash_id', 'mac'].some(k => identity[k] !== expectedIdentity[k])) {
        const error = new Error('physical_identity_changed');
        error.code = 'physical_identity_changed';
        throw error;
      }
      if (!hardware.hold) await hardware.openConsole();
      ctx.identity = identity;
      ctx.localDevice = this.makeDevice(ctx, identity, hardware);
      ctx.status = 'connected';
      // Registration may arrive asynchronously; its generation is assigned
      // by the server, never manufactured by the browser.
      ctx.generation = null;
      ctx.agent = this.createAgent(ctx, hardware);
      ctx.agent.connect();
      if (!expectedIdentity || !ctx.reconnect.port) ctx.reconnect.remember(port, identity);
      this.onState(ctx);
      return ctx;
    } catch (error) {
      try { await hardware.close(); } catch { /* best effort, no retry */ }
      if (ctx.engine === hardware) ctx.engine = null;
      ctx.status = expectedIdentity ? 'usb_lost' : 'disconnected';
      this.onState(ctx);
      throw error;
    }
  }

  // Physical loss of one exact port must not impact another controller.
  lost(port) {
    const ctx = this.contexts.lost(port);
    if (!ctx) return null;
    if (ctx.agent?.active || ctx.agent?.awaitingResult) ctx.engine?.setHold(true);
    ctx.engine?.cancelTrigger();
    ctx.engine?.transport?.abort('closed', 'USB disconnected');
    ctx.agent?.disconnect();
    ctx.reconnect.disconnected(port);
    this.onEvent(ctx, 'usb_lost', {});
    this.onState(ctx);
    return ctx;
  }

  // Voluntary disconnect never schedules a retry, and must not close a
  // resource used by another context. While active, refuse without side effects.
  async disconnect(id) {
    const ctx = this.contexts.require(id);
    if (ctx.busy || ctx.disconnecting || ctx.engine?.busy || ctx.agent?.active ||
        ctx.agent?.awaitingResult || ctx.reconnect?.running) throw new Error('board_not_idle');
    ctx.disconnecting = true;
    this.onState(ctx);
    try {
      ctx.reconnect.forget();
      ctx.agent?.cancelTrigger();
      ctx.agent?.grant(0);
      ctx.agent?.disconnect();
      if (ctx.engine) await ctx.engine.close();
      if (ctx.port.readable || ctx.port.writable) throw new Error('serial_port_still_open');
      ctx.engine = null;
      ctx.agent = null;
      this.contexts.disconnected(id);
      this.onState(ctx);
      return ctx;
    } finally {
      ctx.disconnecting = false;
      this.onState(ctx);
    }
  }

  // Explicit operator action: never automatically use a different port.
  async reconnect(id) {
    const ctx = this.contexts.require(id);
    if (ctx.status !== 'disconnected' && ctx.status !== 'usb_lost') throw new Error('board_not_offline');
    if (ctx.reconnect.waiting || ctx.reconnect.running) throw new Error('reconnect_pending');
    const granted = await this.ports();
    if (!granted.includes(ctx.port)) throw new Error('serial_port_not_granted');
    return this.attach(ctx, ctx.port, ctx.identity);
  }

  cancelReconnect(id) {
    const ctx = this.contexts.require(id);
    ctx.reconnect.stop();
    if (ctx.status === 'reconnecting' || ctx.status === 'usb_lost')
      this.contexts.disconnected(id);
    this.onState(ctx);
  }
}

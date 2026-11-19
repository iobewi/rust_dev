import { normalizeLoaderBaud } from './loader-settings.js';
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
    const loaderBaud = normalizeLoaderBaud(options.loaderBaud);
    const ctx = this.contexts.add(id, port);
    // A per-board snapshot survives engine replacement on USB loss/reconnect.
    ctx.options = { ...options, loaderBaud, manualDownload: options.manualDownload === true || options.romOnly === true, romOnly: options.romOnly === true };
    ctx.reconnect = this.makeReconnect({
      ports: () => this.ports(),
      idle: () => !ctx.busy && !ctx.disconnecting && !ctx.engine?.busy &&
        !ctx.agent?.active && (!ctx.agent?.awaitingResult || ctx.agent?.stopped),
      attach: (exactPort, identity, isCurrent) => this.attach(ctx, exactPort, identity, isCurrent),
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

  async attach(ctx, port, expectedIdentity = null, isCurrent = () => true) {
    const checkCurrent = () => {
      if (!isCurrent()) throw Object.assign(new Error('reconnect_cancelled'), { code: 'reconnect_cancelled' });
    };
    checkCurrent();
    if (ctx.port !== port || this.contexts.ports.get(port) !== ctx.id) throw new Error('serial_port_mismatch');
    if (ctx.busy || ctx.disconnecting || ctx.engine?.busy || ctx.agent?.active ||
        (ctx.agent?.awaitingResult && !ctx.agent?.stopped)) throw new Error('board_not_idle');
    if (ctx.agent) {
      if (ctx.agent.awaitingResult) {
        ctx.engine?.setHold(true);
        ctx.agent.orphanedOperation = ctx.agent.awaitingResult.id;
        ctx.agent.awaitingResult = null;
        ctx.agent.activity();
      }
      ctx.agent.disconnect();
      ctx.agent = null;
    }
    if (ctx.engine) {
      await ctx.engine.close();
      ctx.engine = null;
    }
    checkCurrent();
    // An old engine may deliver a delayed UART/event callback after a
    // disconnect/reconnect. Never forward it to the new agent or console.
    let hardware;
    hardware = this.createEngine(ctx, port, (type, payload) => {
      if (ctx.engine !== hardware || ctx.status === 'usb_lost' ||
          ctx.status === 'disconnected') return;
      if (ctx.status === 'connected') ctx.agent?.event(type, payload);
      this.onEvent(ctx, type, payload);
    });
    ctx.engine = hardware;
    try {
      const identity = await hardware.identity();
      checkCurrent();
      if (expectedIdentity && ['chip', 'flash_size', 'flash_id', 'mac'].some(k => identity[k] !== expectedIdentity[k])) {
        const error = new Error('physical_identity_changed');
        error.code = 'physical_identity_changed';
        throw error;
      }
      if (!hardware.hold) await hardware.openConsole();
      checkCurrent();
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
      ctx.status = isCurrent() && expectedIdentity ? 'usb_lost' : 'disconnected';
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
    // An acknowledged or unacknowledged result belongs to the old generation.
    // It cannot be settled by the new agent. Keep the durable hold and let the
    // broker mark the interrupted operation for reconciliation; never replay it.
    if (ctx.agent?.awaitingResult) {
      ctx.agent.orphanedOperation = ctx.agent.awaitingResult.id;
      ctx.agent.awaitingResult = null;
      ctx.agent.activity();
    }
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
        (ctx.agent?.awaitingResult && !ctx.agent?.stopped) ||
        ctx.reconnect?.running) throw new Error('board_not_idle');
    // An old stopped agent may still report a result after the unplug event.
    // Its generation can no longer settle it; preserve hold before cleanup.
    if (ctx.agent?.awaitingResult && ctx.agent.stopped) {
      ctx.engine?.setHold(true);
      ctx.agent.orphanedOperation = ctx.agent.awaitingResult.id;
      ctx.agent.awaitingResult = null;
      ctx.agent.activity();
    }
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


  // Recover only the named board. No server restart, port substitution or
  // replay of an operation. attach() tears down the old loader and agent,
  // checks the physical identity and creates a fresh agent generation.
  async recoverSession(id, { automatic = false } = {}) {
    const ctx = this.contexts.require(id);
    if (ctx.sessionRecovering) throw new Error('session_recovery_pending');
    if (!['connected', 'recovery_required'].includes(ctx.status))
      throw new Error('board_not_connected');
    // Active commands, pending results and privileged sessions must never be
    // silently recovered. Stop accepting offers on the stale generation.
    if (automatic && (!ctx.options.manualDownload || ctx.engine?.hold ||
        ctx.agent?.grantUntil > (typeof performance !== 'undefined' ? performance.now() : 0))) {
      if (ctx.agent?.active || ctx.agent?.awaitingResult || ctx.engine?.busy)
        ctx.engine?.setHold(true);
      ctx.agent?.disconnect();
      ctx.status = 'recovery_required';
      this.onEvent(ctx, 'recovery', { stage: 'operator_required' });
      this.onState(ctx);
      return false;
    }
    // A stopped generation may still retain an active/pending result. It is
    // not an executing transport: explicit recovery can orphan that result,
    // while retaining the durable hold. Never interrupt live hardware work.
    const staleStopped = !automatic && ctx.status === 'recovery_required' && ctx.agent?.stopped;
    if (ctx.busy || ctx.disconnecting || ctx.engine?.busy ||
        ((ctx.agent?.active || ctx.agent?.awaitingResult) && !staleStopped) ||
        ctx.reconnect?.running) {
      if (automatic) {
        // A suspended operation has an unknown outcome. Preserve the hold,
        // but never close its transport or replay a command.
        if (ctx.agent?.active || ctx.agent?.awaitingResult || ctx.engine?.busy)
          ctx.engine?.setHold(true);
        ctx.agent?.disconnect();
        ctx.status = 'recovery_required';
        this.onEvent(ctx, 'recovery', { stage: 'operation_uncertain' });
        this.onState(ctx);
        return false;
      }
      throw new Error('board_not_idle');
    }
    if (!automatic && ctx.agent?.grantUntil > (typeof performance !== 'undefined' ? performance.now() : 0))
      throw new Error('grant_active');
    ctx.sessionRecovering = true;
    ctx.status = 'reconnecting';
    // Quiesce the stale generation before the first asynchronous step.
    // Otherwise an offer could arrive while getPorts() is pending.
    ctx.agent?.disconnect();
    ctx.engine?.cancelTrigger();
    this.onEvent(ctx, 'recovery', { stage: 'reconnecting' });
    this.onState(ctx);
    try {
      const granted = await this.ports();
      if (!granted.includes(ctx.port)) throw new Error('serial_port_not_granted');
      // Retire only a stopped generation, after validating the exact port.
      // The outcome stays uncertain and hold survives the new engine.
      if (staleStopped && (ctx.agent?.active || ctx.agent?.awaitingResult)) {
        ctx.engine?.setHold(true);
        const pending = ctx.agent.awaitingResult || ctx.agent.active;
        ctx.agent.orphanedOperation = pending?.id || ctx.agent.orphanedOperation;
        ctx.agent.awaitingResult = null;
        ctx.agent.active = false;
        ctx.agent.activity?.();
      }
      // Keep the saved identity and per-board UART/manual options. A new
      // engine cannot inherit stale ESPLoader or WebSocket state.
      await this.attach(ctx, ctx.port, ctx.identity);
      this.onEvent(ctx, 'recovery', { stage: 'ready' });
      return true;
    } catch (error) {
      ctx.status = 'recovery_required';
      this.onEvent(ctx, 'recovery', { stage: 'failed' });
      this.onState(ctx);
      throw error;
    } finally {
      ctx.sessionRecovering = false;
      this.onState(ctx);
    }
  }

  // The browser does not necessarily emit a Web Serial disconnect after
  // host sleep. Treat every connected session as stale, independently.
  async resumeAfterSleep() {
    // Separate ports/controllers are independent: a timed-out ROM on one
    // board must not delay recovery of another.
    return Promise.all(this.list().filter(ctx => ctx.status === 'connected').map(async ctx => {
      try {
        const recovered = await this.recoverSession(ctx.id, { automatic: true });
        return { id: ctx.id, recovered };
      } catch {
        return { id: ctx.id, recovered: false };
      }
    }));
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

  // A cancelled physical reconnect can leave the former engine allocated.
  // Never free an ID/port for reuse until every owned resource is closed.
  async forget(id) {
    const ctx = this.contexts.require(id);
    if (ctx.status !== 'disconnected') throw new Error('board_not_offline');
    await this.disconnect(id);
    const forgotten = this.contexts.forget(id);
    this.onState(forgotten);
    return forgotten;
  }
}

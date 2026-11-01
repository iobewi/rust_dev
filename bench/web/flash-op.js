// Flash operations as the bench sees them: what was sent, what is known about the flash afterwards.
//
// Rule: the stub erases the region BEFORE it acknowledges FLASH_DEFL_BEGIN. From the moment a begin (or a chip
// erase) is sent, a missing answer proves nothing about the flash: the outcome is `indeterminate`, and the
// board must be checked (read-back) before anything boots it. Nothing here retries a command.
import { md5hex } from "./md5.js";

/** Ring buffer of monotonic-timestamped events. Never holds payloads, only names, sizes and durations. */
export class Tracer {
  constructor(env = {}) {
    this.now = env.now || (() => performance.now());
    this.visibility = env.visibility || (() => (typeof document !== "undefined" ? document.visibilityState : "n/a"));
    this.capacity = env.capacity || 400;
    this.events = [];
    this.t0 = this.now();
  }
  ev(name, detail = {}) {
    const e = { t: +(this.now() - this.t0).toFixed(3), vis: this.visibility(), ev: name, ...detail };
    this.events.push(e);
    if (this.events.length > this.capacity) this.events.shift();
    return e;
  }
  snapshot() { return this.events.slice(); }
  line(e) { const { t, vis, ev, ...rest } = e; return `t=${t} vis=${vis} ${ev}${Object.keys(rest).length ? " " + JSON.stringify(rest) : ""}`; }
}

/**
 * Wraps the loader commands that change the flash (and the MD5 check) on this instance. `track` is filled as
 * the commands are sent: a region is "touched" the moment its begin is sent, not when it is acknowledged.
 */
export function instrument(loader, tracer, track) {
  const wrap = (name, before, after) => {
    const orig = loader[name].bind(loader);
    loader[name] = async (...args) => {
      const info = before ? before(...args) : {};
      const t = tracer.now();
      tracer.ev(name + "-start", info);
      try {
        const r = await orig(...args);
        tracer.ev(name + "-end", { ms: +(tracer.now() - t).toFixed(3) });
        if (after) after(r, ...args);
        return r;
      } catch (e) {
        tracer.ev(name + "-error", { ms: +(tracer.now() - t).toFixed(3), kind: e.kind || e.name, msg: String(e.message).slice(0, 160) });
        throw e;
      }
    };
  };
  const region = () => track.regions[track.regions.length - 1];
  wrap("flashDeflBegin", (size, comp, offset) => { track.regions.push({ addr: offset, size, begin: "sent", transfer: "pending", md5: "not-run" }); track.stage = "begin"; return { addr: offset, size, comp }; },
       () => { region().begin = "acknowledged"; track.stage = "data"; });
  wrap("flashBegin", (size, offset) => { track.regions.push({ addr: offset, size, begin: "sent", transfer: "pending", md5: "not-run" }); track.stage = "begin"; return { addr: offset, size }; },
       () => { region().begin = "acknowledged"; track.stage = "data"; });
  wrap("flashDeflBlock", (data, seq) => ({ seq, len: data.length }));
  wrap("flashBlock", (data, seq) => ({ seq, len: data.length }));
  wrap("flashDeflFinish", () => { track.stage = "finish"; return {}; }, () => { region().transfer = "complete"; track.stage = "verify"; });
  wrap("flashFinish", () => { track.stage = "finish"; return {}; }, () => { region().transfer = "complete"; track.stage = "verify"; });
  wrap("eraseFlash", () => { track.chipErase = "sent"; track.stage = "erase"; return {}; }, () => { track.chipErase = "done"; });
  wrap("flashMd5sum", (addr, size) => { track.stage = "md5"; return { addr, size }; });
}

const outcomeOf = (track) => {
  const regions = track.regions.map((r) => ({ ...r }));
  return regions;
};

/**
 * Classifies a failed or interrupted write from what was sent.
 *   failed        nothing was sent that changes the flash, or the content is known to differ: not a success
 *   indeterminate the flash may be erased or half written, or the check itself did not answer
 */
export function classify(error, track) {
  const msg = String((error && error.message) || error);
  const kind = (error && error.kind) || "error";
  const regions = outcomeOf(track);
  const touched = regions.length > 0 || track.chipErase;
  const mismatch = /MD5 of file does not match/i.test(msg);
  const last = regions[regions.length - 1];
  if (mismatch) {
    if (last) last.md5 = "mismatch";
    return { state: "failed", flash: "content-mismatch", failed_at: "md5", md5: "mismatch", transfer: "complete", regions, kind: "md5-mismatch", msg };
  }
  if (!touched) return { state: "failed", flash: "unchanged", failed_at: track.stage || "prepare", md5: "not-run", transfer: "not-started", regions, kind, msg };
  const transferDone = last && last.transfer === "complete" && !track.chipErase;
  if (transferDone && track.stage === "md5") {
    last.md5 = "indeterminate";
    return { state: "indeterminate", flash: "written-unverified", failed_at: "md5", md5: "indeterminate", transfer: "complete", regions, kind, msg };
  }
  return { state: "indeterminate", flash: "possibly-erased", failed_at: track.stage, md5: "not-run", transfer: "incomplete", regions, kind, msg };
}

/**
 * Writes `items` ({path, addr, data, sha}) and returns a structured result; never throws.
 * ok is true only when the whole transfer completed and, with md5 on, the loader's MD5 matched.
 * `verification.sha256_readback` is always "not-run": the independent read-back belongs to the campaign.
 */
export async function runWrite(loader, items, opts = {}) {
  const tracer = opts.tracer || new Tracer();
  const track = { regions: [], stage: "prepare", chipErase: false };
  const md5 = opts.md5 !== false;
  instrument(loader, tracer, track);
  try {
    if (opts.erase) await loader.eraseFlash();
    await loader.writeFlash({
      fileArray: items.map((it) => ({ data: it.data, address: it.addr })),
      flashMode: "keep", flashFreq: "keep", flashSize: "keep", eraseAll: false, compress: true,
      ...(md5 ? { calculateMD5Hash: (image) => md5hex(image) } : {}),
      reportProgress: opts.reportProgress,
    });
    for (const r of track.regions) if (md5) r.md5 = "verified";
    return {
      ok: true, state: "completed", flash: "written",
      verification: { transfer: "complete", md5: md5 ? "verified" : "not-run", sha256_readback: "not-run" },
      items: items.map((it, i) => ({ path: it.path, addr: it.addr, size: it.data.length, sha256: it.sha, transfer: "complete", md5: md5 ? "verified" : "not-run", region: track.regions[i] })),
      requires_check: false,
    };
  } catch (e) {
    const c = classify(e, track);
    tracer.ev("write-" + c.state, { at: c.failed_at, kind: c.kind, flash: c.flash });
    return {
      ok: false, state: c.state, flash: c.flash, error: c.msg, error_kind: c.kind, failed_at: c.failed_at,
      verification: { transfer: c.transfer, md5: c.md5, sha256_readback: "not-run" },
      regions: c.regions.map((r) => ({ addr: "0x" + r.addr.toString(16), size: r.size, begin: r.begin, transfer: r.transfer, md5: r.md5 })),
      requires_check: c.state === "indeterminate" || c.flash === "content-mismatch",
      hold: true,   // no automatic return to the console (a reopen resets the board)
    };
  }
}

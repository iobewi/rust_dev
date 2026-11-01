"""Command lifecycle of the bench: Queued -> Claimed -> Running -> Completed / Failed / Indeterminate, or Expired.

The page owns the serial port and executes; this module only decides what may be handed to it, once.

  Queued         waiting; expires after its ttl and is then never delivered
  Claimed        handed to the page, not started: the page must acknowledge (ack) before doing anything
  Running        acknowledged by the page: execution may have changed the flash from here on
  Completed      result ok
  Failed         result not ok, outcome known (for a flash write: nothing was left half done)
  Indeterminate  the outcome is unknown: the page reported it, or the page that was running it is gone
  Expired        never started (ttl over, or claimed and never acknowledged): certainly NOT executed

Rules: one command in flight (Claimed or Running); a request id exists once; an acknowledgement is valid once
and only for the claim it was issued with; a Running command is never expired nor interrupted because an HTTP
client left: it ends by its result, or as Indeterminate when a different page instance takes over; nothing is
ever re-queued automatically after it was acknowledged. A result outlives the HTTP connection that carried it.
Payloads (tx bytes, request bodies) are never written to the event log or to the status.
"""
import threading
import time
import uuid

TERMINAL = {"completed", "failed", "indeterminate", "expired"}
RESULT_STATES = {"completed", "failed", "indeterminate"}


class Commands:
    def __init__(self, ttl=120.0, ack_timeout=15.0, keep=500, clock=time.monotonic, wall=time.time):
        self.ttl, self.ack_timeout, self.keep = ttl, ack_timeout, keep
        self.clock, self.wall = clock, wall
        self.cond = threading.Condition(threading.Lock())
        self.recs = {}          # req -> record
        self.order = []         # arrival order
        self.events = []        # drained by the caller, written outside the lock

    # ---- helpers (lock held)
    def _event(self, rec, event, **extra):
        self.events.append({"t": round(self.wall(), 3), "req": rec["req"], "op": rec["cmd"].get("op"), "event": event,
                            "state": rec["state"], **extra})

    def _end(self, rec, state, result, reason=None):
        rec["state"] = state
        rec["done_at"] = self.wall()
        if reason:
            rec["reason"] = reason
        rec["result"] = result
        self._event(rec, state, reason=reason)
        self._trim()
        self.cond.notify_all()

    def _synthetic(self, rec, state, executed, reason):
        return {"req": rec["req"], "op": rec["cmd"].get("op"), "ok": False, "state": state, "executed": executed, "reason": reason}

    def _trim(self):
        done = [r for r in self.order if self.recs[r]["state"] in TERMINAL]
        for req in done[:-self.keep] if len(done) > self.keep else []:
            self.order.remove(req)
            del self.recs[req]

    def _sweep(self):
        now = self.clock()
        for req in list(self.order):
            rec = self.recs.get(req)
            if rec is None:
                continue
            if rec["state"] == "queued" and now >= rec["expires"]:
                self._end(rec, "expired", self._synthetic(rec, "expired", False, "ttl-expired-in-queue"), "ttl-expired-in-queue")
            elif rec["state"] == "claimed" and now >= rec["claim_until"]:
                # No ack: the page cannot have started (it acknowledges first). Not replayed.
                self._end(rec, "expired", self._synthetic(rec, "expired", False, "claimed-not-acknowledged"), "claimed-not-acknowledged")

    def _in_flight(self):
        return next((self.recs[r] for r in self.order if self.recs[r]["state"] in ("claimed", "running")), None)

    # ---- API
    def enqueue(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("op"), str) or not body["op"]:
            return 400, {"error": "op required"}
        req = str(body.get("req") or uuid.uuid4().hex)
        ttl = body.get("ttl", self.ttl)
        if isinstance(body.get("expires"), (int, float)):          # legacy: absolute deadline in ms (sender's clock)
            ttl = min(ttl, body["expires"] / 1000.0 - self.wall())
        if not isinstance(ttl, (int, float)):
            return 400, {"error": "ttl must be a number"}
        ttl = min(float(ttl), 3600.0)
        with self.cond:
            if req in self.recs:
                return 409, {"error": "duplicate req", "req": req, "state": self.recs[req]["state"]}
            rec = {"req": req, "cmd": dict(body, req=req), "state": "queued", "queued_at": self.wall(),
                   "expires": self.clock() + max(ttl, 0.0), "claim": None, "page": None}
            self.recs[req] = rec
            self.order.append(req)
            self._event(rec, "queued", ttl=round(ttl, 3))
            self._sweep()
            self.cond.notify_all()
            return 200, {"queued": True, "req": req, "state": rec["state"]}

    def claim(self, page, wait=0.0):
        """Next command for `page`, or None. One in flight: a different page instance resolves what the previous one left."""
        deadline = self.clock() + wait
        with self.cond:
            while True:
                self._sweep()
                busy = self._in_flight()
                if busy and busy["page"] not in (None, page) and page:
                    if busy["state"] == "claimed":
                        self._end(busy, "expired", self._synthetic(busy, "expired", False, "page-replaced-before-start"), "page-replaced-before-start")
                    else:
                        self._end(busy, "indeterminate", self._synthetic(busy, "indeterminate", "started", "page-replaced-during-execution"),
                                  "page-replaced-during-execution")
                    busy = self._in_flight()
                if not busy:
                    for req in self.order:
                        rec = self.recs[req]
                        if rec["state"] == "queued":
                            rec["state"] = "claimed"
                            rec["claim"] = uuid.uuid4().hex
                            rec["page"] = page
                            rec["claim_until"] = self.clock() + self.ack_timeout
                            self._event(rec, "claimed")
                            return dict(rec["cmd"], claim=rec["claim"])
                left = deadline - self.clock()
                if left <= 0:
                    return None
                self.cond.wait(timeout=min(left, 0.5))

    def ack(self, req, claim, page=None):
        with self.cond:
            self._sweep()
            rec = self.recs.get(req)
            if not rec:
                return 404, {"ok": False, "error": "unknown req"}
            if rec["state"] != "claimed" or rec["claim"] != claim:
                return 409, {"ok": False, "state": rec["state"], "executed": False if rec["state"] in ("queued", "expired") else "unknown",
                             "error": "not claimable: " + rec["state"]}
            rec["state"] = "running"
            rec["started_at"] = self.wall()
            self._event(rec, "running")
            return 200, {"ok": True, "state": "running"}

    def release(self, req, claim):
        """The page was handed a command it cannot start now (busy): back to the queue, ttl unchanged. Not started => safe."""
        with self.cond:
            rec = self.recs.get(req)
            if rec and rec["state"] == "claimed" and rec["claim"] == claim:
                rec["state"], rec["claim"], rec["page"] = "queued", None, None
                self._event(rec, "released")
                self.cond.notify_all()
                return 200, {"ok": True}
            return 409, {"ok": False}

    def finish(self, body):
        req = str(body.get("req", ""))
        with self.cond:
            rec = self.recs.get(req)
            if rec is None:                                        # legacy poster: kept as is
                self.recs[req] = rec = {"req": req, "cmd": {"op": body.get("op")}, "state": "completed" if body.get("ok") else "failed",
                                        "queued_at": self.wall(), "expires": 0, "claim": None, "page": None, "legacy": True}
                self.order.append(req)
                self._end(rec, rec["state"], dict(body, state=rec["state"]))
                return 200, {"ok": True}
            state = body.get("state") if body.get("state") in RESULT_STATES else ("completed" if body.get("ok") else "failed")
            ok = bool(body.get("ok")) and state == "completed"
            result = dict(body, ok=ok, state=state, executed=True)
            if rec["state"] == "running":
                self._end(rec, state, result)
                return 200, {"ok": True, "state": state}
            if rec["state"] in ("indeterminate", "expired"):       # late: kept, the verdict does not change
                rec["late_result"] = result
                self._event(rec, "late-result", late_state=state)
                return 200, {"ok": True, "late": True, "state": rec["state"]}
            if rec["state"] in TERMINAL:
                return 200, {"ok": True, "duplicate": True, "state": rec["state"]}
            return 409, {"ok": False, "error": "result for a command not started: " + rec["state"]}

    def result(self, req):
        with self.cond:
            self._sweep()
            rec = self.recs.get(req)
            return dict(rec["result"]) if rec and rec.get("result") else {}

    def status(self, req):
        with self.cond:
            self._sweep()
            rec = self.recs.get(req)
            if not rec:
                return {}
            keys = ("req", "state", "reason", "queued_at", "started_at", "done_at")
            out = {k: rec[k] for k in keys if k in rec}
            out["op"] = rec["cmd"].get("op")
            out["executed"] = {"queued": False, "claimed": False, "expired": False}.get(rec["state"], "started")
            if "late_result" in rec:
                out["late_result"] = {k: rec["late_result"].get(k) for k in ("ok", "state", "error")}
            return out

    def summary(self):
        with self.cond:
            self._sweep()
            counts = {}
            for r in self.order:
                counts[self.recs[r]["state"]] = counts.get(self.recs[r]["state"], 0) + 1
            return counts

    def drain_events(self):
        with self.cond:
            ev, self.events = self.events, []
            return ev

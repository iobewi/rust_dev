"""M40 support (issue #40): multi-card qualification through the public API.

Two things live here and nothing else:
  * pure decision functions (`check_*`) that turn recorded observations into a list of violations - they never touch the
    network, so they are unit-tested against synthetic observations;
  * `BoardWatch`, a NON-INTRUSIVE collector for a card the scenario must not command: it performs only authenticated
    GETs (device state and event pages) from its own client, never a reservation, never a command.

A spontaneous UART event on the observed card is NOT an operation on it: `uart` is the only event kind tolerated there,
and it is only counted. Any other kind (mark, transport, trigger, grant, connected, disconnected, hold, recover) or any
change of agent / generation / connection / quarantine / reservation / grant is a violation. UART payloads are never read.
"""
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from iobewi_bench.client import BenchClient, ContinuityError

OBSERVED_FIELDS = ("agent", "generation", "connected", "quarantined", "reservation", "privileged_seconds")
SPONTANEOUS_KINDS = frozenset({"uart"})


def observed(device):
    """The comparable, non-secret view of a card. `seq` is kept apart: it legitimately grows with UART traffic."""
    return {**{key: device.get(key) for key in OBSERVED_FIELDS}, "seq": device.get("seq")}


# --- pure decisions -----------------------------------------------------------------------------------------------
def check_ready(card, name):
    """Preconditions of one card, stated as problems (empty = ready)."""
    problems = []
    if not card.get("connected"):
        problems.append(f"{name} is not connected")
    if card.get("quarantined"):
        problems.append(f"{name} is quarantined")
    if card.get("reservation") is not None:
        problems.append(f"{name} is reserved")
    if card.get("privileged_seconds"):
        problems.append(f"{name} has an active remote grant")
    if not card.get("agent"):
        problems.append(f"{name} has no registered agent")
    return problems


def check_distinct(first, second):
    """Two cards must have two agents and two generations; sharing either would mean one registration serves both."""
    problems = []
    if first.get("agent") == second.get("agent"):
        problems.append("both cards report the same agent")
    if first.get("generation") == second.get("generation"):
        problems.append("both cards report the same generation")
    return problems


@dataclass
class WatchResult:
    """Everything the collector saw. `pages` keeps the cursor chain (request cursor, next, latest, lost, size, epoch) of every
    event page so that continuity can be re-proven from the evidence alone."""
    baseline: dict
    epoch: str = ""
    samples: list = field(default_factory=list)      # [{"t": monotonic, "wall": unix, **observed}]
    events: list = field(default_factory=list)       # safe fields only: seq, kind, generation (never a payload)
    pages: list = field(default_factory=list)        # [{"after", "next", "latest", "lost", "count", "epoch"}]
    errors: list = field(default_factory=list)       # every sampling failure (a gap cannot prove independence)
    interval: float = 0.5
    terminated: bool = True                          # the collector thread ended before the final sample
    final_sampled: bool = False                      # one last synchronous sample + drain succeeded after termination
    drained: bool = False                            # the last drain reached `latest`
    epoch_changed: bool = False


def check_independent(result):
    """Violations of the independence of the observed card. Empty list = nothing happened to it (spontaneous UART aside).

    State and event KINDS only. Whether the observation itself is trustworthy (epoch, continuity, termination) is
    `check_complete`: an untrustworthy observation is INDETERMINATE, never a pass and never a failure of the card."""
    problems = []
    base = result.baseline
    for sample in result.samples:
        for key in OBSERVED_FIELDS:
            if sample[key] != base[key]:
                problems.append(f"{key} changed on the observed card: {base[key]!r} -> {sample[key]!r}")
    foreign = sorted({event["kind"] for event in result.events if event["kind"] not in SPONTANEOUS_KINDS})
    if foreign:
        problems.append(f"non-UART event kinds on the observed card: {foreign}")
    return sorted(set(problems))


def check_sequence(result):
    """Integrity of the event capture of the observed card: pages chained from the baseline cursor, no loss, no duplicate,
    no gap, no reordering, one epoch, and a final state that agrees with the last page. Empty list = the capture is complete."""
    problems = []
    base_seq = result.baseline["seq"]
    if not result.pages:
        return ["no event page was read from the observed card"]
    expected_after = base_seq
    for index, page in enumerate(result.pages):
        if page["epoch"] != result.epoch:
            problems.append(f"page {index}: server epoch changed ({result.epoch[:8]} -> {str(page['epoch'])[:8]})")
        if page["lost"]:
            problems.append(f"page {index}: the server reports {page['lost']} lost event(s)")
        if page["after"] != expected_after:
            problems.append(f"page {index}: requested after={page['after']}, expected {expected_after} (cursor chain broken)")
        if page["next"] < page["after"]:
            problems.append(f"page {index}: next {page['next']} is before the cursor {page['after']}")
        expected_after = page["next"]
    seqs = [event["seq"] for event in result.events]
    previous = base_seq
    for seq in seqs:
        if seq == previous:
            problems.append(f"duplicate event seq {seq}")
        elif seq < previous:
            problems.append(f"event seq {seq} arrived after {previous} (reordered)")
        elif seq != previous + 1:
            problems.append(f"event gap: {previous} -> {seq}")
        previous = max(previous, seq)
    if sum(page["count"] for page in result.pages) != len(seqs):
        problems.append("the pages announce a different number of events than were collected")
    last = result.pages[-1]
    if result.final_sampled:
        if not result.drained or last["next"] < last["latest"]:
            problems.append("the final drain did not reach the latest event")
        elif previous != last["latest"]:
            problems.append(f"final state: last event seq {previous} differs from the server's latest {last['latest']}")
        final_seq = result.samples[-1]["seq"] if result.samples else None
        if final_seq is not None and final_seq > last["latest"]:
            problems.append(f"final sample seq {final_seq} is beyond the last page's latest {last['latest']}")
    return sorted(set(problems))


def check_complete(result, *, minimum_samples=3):
    """Can this observation establish anything? Errors, a collector that did not terminate, a missing final sample, an epoch
    change or a broken event capture make it INDETERMINATE for the caller. Never a pass."""
    problems = [f"observation error: {error}" for error in result.errors]
    if not result.terminated:
        problems.append("the collector thread did not terminate: its last reads cannot be trusted")
    if not result.final_sampled:
        problems.append("no final synchronous sample of the observed card")
    if result.epoch_changed:
        problems.append("the server epoch changed during the observation")
    problems += [f"event capture: {p}" for p in check_sequence(result)]
    if len(result.samples) < minimum_samples:
        problems.append(f"only {len(result.samples)} sample(s) of the observed card (need {minimum_samples})")
    return problems


def spontaneous_uart(result):
    """Count only: spontaneous UART traffic of the observed card is reported, never attributed to an operation."""
    return sum(1 for event in result.events if event["kind"] in SPONTANEOUS_KINDS)


def evidence_rows(result):
    """Private, timestamped record of the observation: no UART payload, no raw agent id (a short digest is comparable),
    no reservation owner. One JSON object per row."""
    def agent(value):
        return hashlib.sha256(str(value).encode()).hexdigest()[:12] if value else None
    rows = [{"type": "baseline", "epoch": result.epoch, "interval_s": result.interval,
             **{k: (agent(v) if k == "agent" else ("reserved" if k == "reservation" and v else v)) for k, v in result.baseline.items()}}]
    for sample in result.samples:
        rows.append({"type": "sample", "t": round(sample["t"], 6), "wall": round(sample.get("wall", 0), 3),
                     "agent": agent(sample["agent"]), "generation": sample["generation"], "connected": sample["connected"],
                     "quarantined": sample["quarantined"], "reserved": sample["reservation"] is not None,
                     "privileged_seconds": sample["privileged_seconds"], "seq": sample["seq"]})
    rows += [{"type": "page", **page} for page in result.pages]
    rows += [{"type": "event", "seq": e["seq"], "kind": e["kind"], "generation": e["generation"]} for e in result.events]
    rows += [{"type": "error", "text": error} for error in result.errors]
    rows.append({"type": "summary", "samples": len(result.samples), "pages": len(result.pages), "events": len(result.events),
                 "terminated": result.terminated, "final_sampled": result.final_sampled, "drained": result.drained,
                 "epoch_changed": result.epoch_changed, "spontaneous_uart": spontaneous_uart(result)})
    return rows


def write_evidence(path, result):
    """Write `evidence_rows` as JSON lines, readable by the owner only - at no moment by anyone else.

    The content is serialised FIRST (a serialisation error creates no file). The file is created with mode 0600 and, whether it is
    new or pre-existing with wider permissions, restricted with fchmod BEFORE any content is written or the old content truncated.
    Errors propagate as before (OSError / TypeError); nothing is retried."""
    data = "".join(json.dumps(row, sort_keys=True) + "\n" for row in evidence_rows(result)).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


# --- bench / recipe compatibility ---------------------------------------------------------------------------------
BENCH_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = 1


def local_server_sha256(root=BENCH_ROOT):
    """The digest the server publishes as `server_sha256`, recomputed from this checkout (same algorithm, same files)."""
    digest = hashlib.sha256()
    for path in sorted(list((Path(root) / "server").glob("*.py")) + list((Path(root) / "protocol").glob("*.py"))):
        if path.is_file():
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def assess_server_build(health, *, local=None, accepted=()):
    """Is the RUNNING server compatible with the code of this recipe? Returns (compatible, basis, reasons).

    A different Git SHA is neither accepted blindly nor refused: what matters is the server CODE. Compatible only when
      * the protocol version is the one the recipe speaks, and
      * the server's published `server_sha256` equals the one recomputed from this checkout (same server code, whatever
        later commits changed elsewhere), or is listed, WITH a written justification, by the operator in the private session's `m40.compatible_server_sha256` after review.
    Anything else (or a health document without a build digest) is incompatible and the scenario is BLOCKED."""
    local = local or local_server_sha256()
    health = health or {}
    build = health.get("build") or {}
    reasons = []
    if health.get("protocol") != PROTOCOL:
        reasons.append(f"protocol {health.get('protocol')!r}, the recipe speaks {PROTOCOL}")
    digest = build.get("server_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        reasons.append("the server publishes no server_sha256 build digest")
        return False, "no-digest", reasons
    if reasons:
        return False, "protocol", reasons
    if digest == local:
        return True, "same-server-code", []
    if digest in set(accepted):
        return True, "operator-declared", []
    return False, "different-server-code", [f"running server code {digest[:12]} differs from this checkout's {local[:12]} and is not in "
                                             "m40.compatible_server_sha256 (restart the server on the reviewed code, or declare it after review)"]


def check_operation(operation, *, device, generation, command, args):
    """The settled operation must be the one asked for, on the expected card and generation, and own its artifact."""
    problems = []
    for key, expected in (("device", device), ("generation", generation), ("command", command)):
        if operation.get(key) != expected:
            problems.append(f"operation {key} is {operation.get(key)!r}, expected {expected!r}")
    if operation.get("state") != "completed":
        problems.append(f"operation state is {operation.get('state')!r}")
    artifact = (operation.get("result") or {}).get("artifact")
    if not isinstance(artifact, dict) or not artifact.get("id"):
        problems.append("the operation result carries no artifact")
    elif artifact.get("size") != args["size"]:
        problems.append(f"artifact size {artifact.get('size')!r} differs from the requested {args['size']}")
    return problems


def check_commands(rows, expected):
    """`commands.jsonl` of the reservation: exactly the expected command names, in order, once each (no replay).

    `expected` is a list of (command, args-or-None). A replay or an unrequested command shows up as an extra or
    out-of-order row, whatever the server or the client did."""
    problems = []
    if [row.get("command") for row in rows] != [name for name, _ in expected]:
        problems.append(f"commands submitted {[row.get('command') for row in rows]}, expected {[n for n, _ in expected]}")
        return problems
    if [row.get("seq") for row in rows] != list(range(len(rows))):
        problems.append("submission sequence is not 0..n-1")
    for row, (name, args) in zip(rows, expected):
        if args is not None and {k: row.get("args", {}).get(k) for k in args} != args:
            problems.append(f"{name} was submitted with different arguments than requested")
    return problems


def check_continuity(events, generation):
    """Continuity of the TARGET card: a disconnect or a connection under another generation interrupts the proof window.

    A lost UART source is reported by the caller as a count, not as a violation: entering the loader legitimately stops the console capture."""
    problems = []
    for event in events:
        if event["kind"] == "disconnected":
            problems.append(f"disconnected event (seq {event['seq']}) during the run")
        elif event["kind"] == "connected" and event.get("generation") != generation:
            problems.append(f"connected event (seq {event['seq']}) under another generation")
    return problems


def drain_events(client, device_id, after, *, epoch):
    """Safe fields of every event after a cursor (GET only). Server loss or an epoch change makes the proof INDETERMINATE."""
    events = []
    for _ in range(100):
        batch = client._request("GET", f"/api/v1/devices/{quote(device_id, safe='')}/events?after={after}")
        if batch["epoch"] != epoch or batch["lost"]:
            raise ContinuityError("server epoch changed or events were lost while draining the target card")
        events.extend({k: e.get(k) for k in ("seq", "kind", "generation", "source_lost")} for e in batch["events"])
        after = batch["next"]
        if after >= batch["latest"]:
            return events
    raise ContinuityError("event capture exceeded 100 pages")


def read_commands(directory):
    path = Path(directory) / "commands.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


def verify_private_dump(directory, *, address, size, sha256):
    """Re-read the PRIVATE copy from disk and compare size and SHA-256 with the expected digest; restrict it to its owner.

    Returns (digest, problems). The content is never returned, printed or logged."""
    path = Path(directory) / f"flash-001-{address:08x}.bin"
    if not path.is_file():
        return None, [f"private dump {path.name} is missing"]
    os.chmod(path, 0o600)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    problems = []
    if len(raw) != size:
        problems.append(f"private dump is {len(raw)} bytes, expected {size}")
    if digest != sha256:
        problems.append("private dump SHA-256 differs from the artifact SHA-256")
    return digest, problems


# --- non-intrusive collector --------------------------------------------------------------------------------------
class BoardWatch:
    """Polls ONE card with GET requests only, from its own client. Never reserves, never submits.

    Exactly one reader touches the event cursor at any time: the background thread while it runs, then - only after it has
    terminated - one final synchronous cycle. A thread that does not terminate is reported (INDETERMINATE) and NO final
    read is attempted, so two readers can never share the cursor."""

    PAGES_PER_CYCLE = 50

    def __init__(self, url, token, device_id, *, interval=0.5, client=None, join_timeout=15.0):
        self.device_id, self.interval, self.join_timeout = device_id, interval, join_timeout
        self.client = client or BenchClient(url, token, request_timeout=5)
        self._stop, self._thread, self._after = threading.Event(), None, None
        self._lock = threading.Lock()
        self.result = None

    def _page(self):
        return self.client._request("GET", f"/api/v1/devices/{quote(self.device_id, safe='')}/events?after={self._after}")

    def _cycle(self):
        """One sample, then drain the event pages. Called only by the single current reader (guarded by a lock)."""
        with self._lock:
            result = self.result
            result.samples.append({"t": time.monotonic(), "wall": time.time(), **observed(self.client.device(self.device_id))})
            if result.epoch_changed:
                return                                   # the old cursor means nothing after an epoch change: stop reading events
            result.drained = False
            for _ in range(self.PAGES_PER_CYCLE):
                batch = self._page()
                events = batch["events"]
                result.pages.append({"after": self._after, "next": batch["next"], "latest": batch["latest"], "lost": batch["lost"],
                                     "count": len(events), "epoch": batch["epoch"]})
                if batch["epoch"] != result.epoch:
                    result.epoch_changed = True
                    result.errors.append(f"server epoch changed on an event page ({result.epoch[:8]} -> {str(batch['epoch'])[:8]})")
                    return
                if batch["lost"]:
                    result.errors.append(f"the server reports {batch['lost']} lost event(s) of the observed card")
                result.events.extend({"seq": e["seq"], "kind": e["kind"], "generation": e.get("generation")} for e in events)
                self._after = batch["next"]
                if self._after >= batch["latest"]:
                    result.drained = True
                    return
            result.errors.append("event drain exceeded the page budget of one cycle")

    def _poll(self):
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as error:     # noqa: BLE001 - recorded: an observation gap is INDETERMINATE, never ignored
                self.result.errors.append(f"{type(error).__name__}: {str(error)[:120]}")
            self._stop.wait(self.interval)

    def start(self):
        """Capture the epoch and the baseline synchronously, then observe in the background."""
        epoch = self.client.health()["epoch"]
        baseline = observed(self.client.device(self.device_id))
        self._after = baseline["seq"]
        self.result = WatchResult(baseline=baseline, epoch=epoch, interval=self.interval)
        self._thread = threading.Thread(target=self._poll, name=f"watch-{self.device_id}", daemon=True)
        self._thread.start()
        return baseline

    def stop(self):
        """Ask the collector to end, wait for it, then (and only then) take one final synchronous sample and drain.

        Idempotent. If the thread is still alive after `join_timeout` the result is marked as not terminated and no final read
        is made."""
        if self.result.final_sampled or not self.result.terminated:
            return self.result
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.join_timeout)
            if self._thread.is_alive():
                self.result.terminated = False
                self.result.errors.append(f"collector thread still running after {self.join_timeout}s: no final read attempted")
                return self.result
        try:
            self._cycle()
            self.result.final_sampled = True
        except Exception as error:         # noqa: BLE001
            self.result.errors.append(f"final sample {type(error).__name__}: {str(error)[:120]}")
        return self.result

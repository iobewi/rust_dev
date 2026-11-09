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
    baseline: dict
    samples: list = field(default_factory=list)      # [{"t": monotonic seconds, **observed}]
    events: list = field(default_factory=list)       # safe fields only: seq, kind, generation
    errors: list = field(default_factory=list)       # every sampling failure (a gap cannot prove independence)
    interval: float = 0.5


def check_independent(result):
    """Violations of the independence of the observed card. Empty list = nothing happened to it (spontaneous UART aside).

    An incomplete observation is reported separately by `check_complete`: it is INDETERMINATE, never a pass."""
    problems = []
    base = result.baseline
    for sample in result.samples:
        for key in OBSERVED_FIELDS:
            if sample[key] != base[key]:
                problems.append(f"{key} changed on the observed card: {base[key]!r} -> {sample[key]!r}")
    foreign = sorted({event["kind"] for event in result.events if event["kind"] not in SPONTANEOUS_KINDS})
    if foreign:
        problems.append(f"non-UART event kinds on the observed card: {foreign}")
    if result.samples and result.samples[-1]["seq"] is not None and base["seq"] is not None:
        grew = result.samples[-1]["seq"] - base["seq"]
        if grew < 0:
            problems.append("event sequence of the observed card went backwards")
        elif grew != len(result.events) and not result.errors:
            problems.append(f"sequence grew by {grew} but {len(result.events)} events were collected (events missed)")
    return sorted(set(problems))


def check_complete(result, *, minimum_samples=3):
    """An observation with errors or too few samples cannot establish anything: the caller must not call it a PASS."""
    problems = [f"observation error: {error}" for error in result.errors]
    if len(result.samples) < minimum_samples:
        problems.append(f"only {len(result.samples)} sample(s) of the observed card (need {minimum_samples})")
    return problems


def spontaneous_uart(result):
    """Count only: spontaneous UART traffic of the observed card is reported, never attributed to an operation."""
    return sum(1 for event in result.events if event["kind"] in SPONTANEOUS_KINDS)


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
    """Polls ONE card with GET requests only, from its own client. Never reserves, never submits."""

    def __init__(self, url, token, device_id, *, interval=0.5, client=None):
        self.device_id, self.interval = device_id, interval
        self.client = client or BenchClient(url, token, request_timeout=5)
        self._stop, self._thread, self._after = threading.Event(), None, None
        self.result = None

    def _page(self):
        return self.client._request("GET", f"/api/v1/devices/{quote(self.device_id, safe='')}/events?after={self._after}")

    def _poll(self):
        result = self.result
        while not self._stop.is_set():
            try:
                result.samples.append({"t": time.monotonic(), **observed(self.client.device(self.device_id))})
                for _ in range(20):
                    batch = self._page()
                    if batch["lost"]:
                        result.errors.append("server events of the observed card were lost")
                    result.events.extend({"seq": e["seq"], "kind": e["kind"], "generation": e.get("generation")} for e in batch["events"])
                    self._after = batch["next"]
                    if self._after >= batch["latest"]:
                        break
            except Exception as error:     # noqa: BLE001 - recorded: an observation gap is INDETERMINATE, never ignored
                result.errors.append(f"{type(error).__name__}: {str(error)[:120]}")
            self._stop.wait(self.interval)

    def start(self):
        """Capture the baseline synchronously, then observe in the background."""
        baseline = observed(self.client.device(self.device_id))
        self._after = baseline["seq"]
        self.result = WatchResult(baseline=baseline, interval=self.interval)
        self._thread = threading.Thread(target=self._poll, name=f"watch-{self.device_id}", daemon=True)
        self._thread.start()
        return baseline

    def stop(self):
        """Final synchronous sample after the thread ends, so the last state is always part of the proof."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 10)
        try:
            self.result.samples.append({"t": time.monotonic(), **observed(self.client.device(self.device_id))})
            for _ in range(20):
                batch = self._page()
                self.result.events.extend({"seq": e["seq"], "kind": e["kind"], "generation": e.get("generation")} for e in batch["events"])
                self._after = batch["next"]
                if self._after >= batch["latest"]:
                    break
        except Exception as error:         # noqa: BLE001
            self.result.errors.append(f"final sample {type(error).__name__}: {str(error)[:120]}")
        return self.result

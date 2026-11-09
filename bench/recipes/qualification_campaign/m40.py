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
import base64
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


EVENT_FIELDS = ("seq", "kind", "generation", "source_lost")


def drain_events(client, device_id, after, *, epoch, fields=EVENT_FIELDS):
    """Selected fields of every event after a cursor (GET only). Server loss or an epoch change makes the proof INDETERMINATE.

    `fields` lets a caller also keep `detail` (marks, transport) or the UART arrival data (`at`, `data`, `source_seq`): the payload is only
    ever analysed in memory by the callers, never written to evidence."""
    events = []
    for _ in range(100):
        batch = client._request("GET", f"/api/v1/devices/{quote(device_id, safe='')}/events?after={after}")
        if batch["epoch"] != epoch or batch["lost"]:
            raise ContinuityError("server epoch changed or events were lost while draining the card")
        events.extend({k: e.get(k) for k in fields} for e in batch["events"])
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


# --- M40-12a: simultaneous reservations and marks ------------------------------------------------------------------
def mark_details(events):
    return [event.get("detail") for event in events if event["kind"] == "mark"]


def check_marks(events, expected):
    """The `mark` events of ONE card are exactly the labels submitted to it, once each: none lost, none duplicated, none foreign."""
    seen, problems = mark_details(events), []
    if sorted(seen) != sorted(expected):
        problems.append(f"mark events {sorted(seen)!r}, expected exactly {sorted(expected)!r}")
    elif len(set(seen)) != len(seen):
        problems.append("a mark event was duplicated")
    return problems


def check_event_kinds(events, allowed):
    foreign = sorted({event["kind"] for event in events} - set(allowed))
    return [f"unexpected event kinds {foreign}"] if foreign else []


def check_contiguous(events, base_seq):
    """Event sequence numbers run base+1, base+2, ... with no gap, duplicate or reordering (whatever their kinds)."""
    problems, expected = [], base_seq + 1
    for event in events:
        if event["seq"] != expected:
            problems.append(f"event seq {event['seq']} where {expected} was expected")
            expected = event["seq"]
        expected += 1
    return problems[:5]


def check_reservations(first, second, *, first_held, second_held):
    """Reservation state of two cards against what the scenario step expects (each flag: the card must / must not be reserved)."""
    problems = []
    for name, card, held in (("first", first, first_held), ("second", second, second_held)):
        if (card.get("reservation") is not None) != held:
            problems.append(f"{name} card reservation is {'absent' if held else 'present'}, expected {'present' if held else 'absent'}")
    return problems


def check_mark_operation(operation, *, device, generation, label):
    """A settled `mark` operation: the card and generation it was asked for, completed, and its own label echoed back."""
    problems = []
    for key, expected in (("device", device), ("generation", generation), ("command", "mark")):
        if operation.get(key) != expected:
            problems.append(f"mark operation {key} is {operation.get(key)!r}, expected {expected!r}")
    if operation.get("state") != "completed":
        problems.append(f"mark operation state is {operation.get('state')!r}")
    if (operation.get("result") or {}).get("label") != label:
        problems.append("the mark result does not echo the submitted label")
    return problems


# --- M40-11c: known content ----------------------------------------------------------------------------------------
CHIP_IDS = {"esp32s3": 9, "esp32c3": 5}


def check_image_header(raw, chip):
    """Independent check of an ESP image header at the start of `raw`: magic 0xE9, a plausible segment count and the chip id
    of the declared chip (bytes 12..13, little endian). Returns (facts, problems); no payload is returned."""
    problems, facts = [], {}
    if len(raw) < 24:
        return facts, ["fewer than 24 bytes"]
    facts = {"magic": raw[0], "segments": raw[1], "chip_id": int.from_bytes(raw[12:14], "little")}
    if raw[0] != 0xE9:
        problems.append(f"image magic is {raw[0]:#04x}, expected 0xe9")
    if not 1 <= raw[1] <= 16:
        problems.append(f"implausible segment count {raw[1]}")
    if chip not in CHIP_IDS:
        problems.append(f"no known chip id for {chip!r}")
    elif facts["chip_id"] != CHIP_IDS[chip]:
        problems.append(f"chip id {facts['chip_id']}, expected {CHIP_IDS[chip]} for {chip}")
    return facts, problems


def is_erased(raw):
    return bool(raw) and raw.count(0xFF) == len(raw)


def app_partitions(rows):
    """Application partitions of the API view (type 0), in address order."""
    return sorted((r for r in rows if r.get("type") == 0), key=lambda r: r["address"])


def check_table_agreement(api_rows, decoded):
    """API view of the partition table against an independent decode of the raw bytes: (label, address, size), same order."""
    mine = [(r["label"], r["address"], r["size"]) for r in api_rows]
    return [] if mine == list(decoded) else [f"API table {mine!r} differs from the independent decode {list(decoded)!r}"]


# --- M40-11d: ROM / second stage / application markers --------------------------------------------------------------
def decode_chunks(events):
    """UART chunks [(seq, bytes)] of a list of events (payload kept in memory only)."""
    return [(e["seq"], base64.b64decode(e["data"], validate=True)) for e in events if e["kind"] == "uart" and e.get("data")]


def locate_markers(chunks, markers):
    """Find ordered markers [(name, bytes)] in the UART stream: each one searched AFTER the previous one's end.

    Returns a list of {"name", "found", "offset", "seq", "anywhere"}: `found` is the ordered result, `anywhere` tells that the marker exists
    somewhere in the stream even if out of order. Only names, offsets and sequence numbers are returned, never UART text."""
    stream = b"".join(chunk for _, chunk in chunks)
    starts, total = [], 0
    for seq, chunk in chunks:
        starts.append((total, seq))
        total += len(chunk)

    def seq_at(offset):
        found = None
        for start, seq in starts:
            if start <= offset:
                found = seq
        return found
    result, position = [], 0
    for name, pattern in markers:
        index = stream.find(pattern, position)
        if index >= 0:
            result.append({"name": name, "found": True, "offset": index, "seq": seq_at(index), "anywhere": True})
            position = index + len(pattern)
        else:
            result.append({"name": name, "found": False, "offset": None, "seq": None, "anywhere": pattern in stream})
            position = len(stream) + 1               # nothing can be ordered after a missing marker
    return result


def marker_problems(located):
    problems = []
    for item in located:
        if not item["found"]:
            problems.append(f"{item['name']} marker " + ("seen only out of order" if item["anywhere"] else "never seen"))
    return problems


# --- M40-4: simultaneous UART of two cards -------------------------------------------------------------------------
def uart_activity(events):
    uart = [e for e in events if e["kind"] == "uart" and e.get("data")]
    if not uart:
        return {"events": 0, "bytes": 0, "first_at": None, "last_at": None}
    return {"events": len(uart), "bytes": sum(len(base64.b64decode(e["data"], validate=True)) for e in uart),
            "first_at": min(e["at"] for e in uart), "last_at": max(e["at"] for e in uart)}


def window_overlap(first, second):
    """Seconds during which both cards' UART activity windows overlap (0 when one is silent or they are disjoint)."""
    if first["first_at"] is None or second["first_at"] is None:
        return 0.0
    return max(0.0, min(first["last_at"], second["last_at"]) - max(first["first_at"], second["first_at"]))


def interleavings(first, second):
    """Number of times the arrival order switches from one card to the other: two sources received at the same time alternate."""
    merged = sorted([(e["at"], 0) for e in first if e["kind"] == "uart"] + [(e["at"], 1) for e in second if e["kind"] == "uart"])
    return sum(1 for a, b in zip(merged, merged[1:]) if a[1] != b[1])


def check_uart_continuity(events):
    """Per card: seq strictly increasing, source_seq contiguous, no flagged source loss. Returns problems."""
    problems, previous, last_source = [], None, None
    for e in events:
        if previous is not None and e["seq"] <= previous:
            problems.append(f"event seq {e['seq']} after {previous}")
        previous = e["seq"]
        if e["kind"] != "uart":
            continue
        if e.get("source_lost"):
            problems.append(f"source loss flagged at seq {e['seq']}")
        if last_source is not None and e.get("source_seq") != last_source + 1:
            problems.append(f"source_seq jump {last_source} -> {e.get('source_seq')} at seq {e['seq']}")
        last_source = e.get("source_seq")
    return problems[:5]


def check_uart_isolation(streams, own_markers):
    """`streams` {card: events}, `own_markers` {card: [bytes]} (each card's distinctive lines, disjoint across cards).

    Every card's stream must contain at least one of its own markers and none of the other cards': no mixing, and a proof that each
    source really is the card it is attributed to."""
    problems = []
    data = {card: b"".join(chunk for _, chunk in decode_chunks(events)) for card, events in streams.items()}
    for card, markers in own_markers.items():
        if not any(marker in data[card] for marker in markers):
            problems.append(f"{card}: none of its own markers was received")
        for other, foreign in own_markers.items():
            if other != card and any(marker in data[card] for marker in foreign):
                problems.append(f"{card}: received a marker belonging to {other} (mixed streams)")
    return problems


# --- M40-4 reset_both: concurrent commands and their observation ----------------------------------------------------
ACTIVE_KINDS = frozenset({"uart", "operation", "transport"})      # events a stimulated (reset) card may legitimately produce


@dataclass
class FireResult:
    errors: dict = field(default_factory=dict)       # card -> exception raised by its command (never retried)
    alive: list = field(default_factory=list)        # cards whose command thread was still running after the join deadline


def fire_concurrently(boards, command, *, join_timeout, **kwargs):
    """Submit ONE `command` per Board from one daemon thread each, all started together, then wait up to `join_timeout` seconds in total.

    The threads' termination is checked, not assumed: a card whose thread is still alive is listed in `alive` and its outcome is UNKNOWN
    (the caller must report INDETERMINATE, keep the reservation and never resubmit). Exceptions raised inside a thread are returned,
    never swallowed and never retried."""
    result, threads = FireResult(), {}

    def run(name, board):
        try:
            board.command(command, **kwargs)
        except BaseException as error:              # noqa: BLE001 - returned to the caller, never retried
            result.errors[name] = error
    for name, board in boards.items():
        threads[name] = threading.Thread(target=run, args=(name, board), name=f"fire-{name}", daemon=True)
    for thread in threads.values():
        thread.start()
    deadline = time.monotonic() + join_timeout
    for thread in threads.values():
        thread.join(max(0.0, deadline - time.monotonic()))
    result.alive = [name for name, thread in threads.items() if thread.is_alive()]
    return result


def stimulated_continuity(result):
    """While a card is stimulated its reservation legitimately appears and disappears, but its agent, generation and connection must not
    change: a change is a CONTINUITY loss (USB re-enumeration, agent re-registration), reported INDETERMINATE by the caller."""
    problems, base = [], result.baseline
    for sample in result.samples:
        for key in ("agent", "generation", "connected"):
            if sample[key] != base[key]:
                problems.append(f"{key} changed on a stimulated card: {base[key]!r} -> {sample[key]!r}")
    return sorted(set(problems))


def stimulated_violations(result):
    """Violations that are not continuity losses: quarantine or a remote grant appearing, or an event kind a reset must never produce."""
    problems, base = [], result.baseline
    for sample in result.samples:
        for key in ("quarantined", "privileged_seconds"):
            if sample[key] != base[key]:
                problems.append(f"{key} changed on a stimulated card: {base[key]!r} -> {sample[key]!r}")
    foreign = sorted({event["kind"] for event in result.events if event["kind"] not in ACTIVE_KINDS})
    if foreign:
        problems.append(f"unexpected event kinds on a stimulated card: {foreign}")
    return sorted(set(problems))


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

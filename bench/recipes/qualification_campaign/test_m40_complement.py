"""M40 complement (issue #40): M40-12a simultaneous reservations, M40-11c/d known content and application return, M40-4 simultaneous UART.

Same architecture as test_m40_multiboard.py: pytest -> official client -> REST API; the browser only owns the ports. No write, no erase,
no grant, no server change. Every scenario needs its own consent in the PRIVATE session; nothing is implied by --bench.

M40-12a  (consent M40-12A)  both cards reserved at the same time by one client; ONE `mark` per card with a distinct label. `mark` is an event
         emission in the page agent: no loader, no reset, no flash access, no grant (docs/api.md, esp-engine.js). Expected `seq` growth is
         NOT constant: the marks and their operation events are expected and counted.
  PASS           both reservations visible together; each operation targets its own card and generation; each card's events contain exactly its
                 own mark(s); releasing the first card leaves the second one reserved and usable (a second mark on it); the first card gets no
                 event for it; afterwards no reservation, no quarantine, no disconnect, no new generation, no lost event; the event sequence of
                 each card is contiguous; exactly [mark] / [mark, mark] were submitted (no replay);
  FAIL           a mark lands on the other card, a reservation of one card blocks or releases the other, a generation or state changes;
  BLOCKED        no consent, a card not ready or without the `mark` capability, a different chip;
  INDETERMINATE  an unresolved operation or a lost proof window (continuity / epoch / lost events).
M40-11c/d (consent M40-11C) on the FIRST card only, one loader cycle: known content, then the application's return.
  11c PASS       the bootloader header (address 0) is a real ESP image header of the declared chip, read twice with equal digests; the partition table
                 declared in m40.known.table_offset decodes independently to exactly the API view; every application partition's first 4 KiB read
                 twice with equal digests, at least one is a valid image header and any other is entirely erased; an all-0xFF read is never a proof
                 of correct addressing;
  11d PASS       after the cursor taken BEFORE the `console` command: the ROM, the second-stage bootloader and the application markers DECLARED in the
                 private session are received IN THAT ORDER within m40.known.marker_timeout_s, with no continuity break;
  FAIL           either criterion is violated (e.g. a marker never seen or out of order: the application did not demonstrably restart);
  BLOCKED        consent, section, a placeholder application marker, no valid table at the declared offset; INDETERMINATE as in M40-11a.
  The second card is only observed (GET), exactly as in M40-11a.
  Evidence (m40-11c-reads.json, m40-11d-markers.json) is written in a `finally`, in EVERY verdict, with the phase reached and the type of the
  error in flight; an evidence problem is an incident and never replaces that error (on an otherwise clean run it makes the test fail).
  A failing `console` is never retried; its restoration is UNKNOWN (the card may still be in the loader).
M40-4    simultaneous UART of two cards (m40.uart; stimulus 'none' = passive capture of what the firmwares already print; 'reset_both' = one `reset`
         per card at the same time, which needs consent M40-4-RESET AND --qual-allow-reset: a reset of the second card is never implied):
  PASS           each card's own distinctive markers (disjoint) are received on its stream and none of the other's; at least m40.uart.min_bytes per
                 card; the arrival windows overlap for at least min_overlap_s and the arrival order alternates at least min_interleavings times;
                 per card seq strictly increases, source_seq is contiguous and no source loss is flagged; no generation / agent / connection /
                 quarantine change and no reservation left. BOTH cards are observed by GET from before anything happens, in both modes, with the
                 same observation integrity (one epoch on every event page, no lost / duplicated / missing event, collector terminated, final
                 sample). What differs: PASSIVE forbids any change of either card (reservations and non-UART events included); ACTIVE
                 (reset_both) expects the two reservations and the operation events, but an agent / generation / connection change is a loss of
                 continuity (INDETERMINATE) and a quarantine, a grant or an event kind a reset must not produce is a FAIL;
  INDETERMINATE  (reset_both) a reset command thread still running after its deadline: the outcome is unknown, the reservations are KEPT
                 (never released, never resubmitted), as for any unresolved operation;
  BLOCKED        passive capture with a silent card: the bench does not provide the SOURCE (as U8);
  FAIL           mixed streams, missing own marker, no overlap or alternation, a continuity violation; INDETERMINATE: epoch change, lost events,
                 a generation change after a stimulus (USB re-enumeration), an unresolved operation.
UART payloads are analysed in memory only; the evidence keeps counts, windows, offsets and digests, never text. Hardware status: NOT executed.
"""
import hashlib
import json
import os
import time
import uuid

import pytest

from iobewi_bench.client import ContinuityError, PendingOperation

from . import m40
from .harness import Blocked, Outcome, make_campaign, recorded
from .readonly import (_console_after_early_exit, check_rows, decode_table, probe_candidates, record_transport, require_consent,
                       require_target, retained_reads, start_or_block)
from .session import build_manifest
from .test_m40_multiboard import LIMITS, TARGET_CHIP, _second_card, _second_card_id, _server_compat

pytestmark = pytest.mark.bench
LIMITS_UART = LIMITS + "; UART arrival times are the server's, not the cards'"
MARK_KINDS = {"mark", "operation", "uart"}          # no loader, no reset: no transport / connected / disconnected / grant / hold event
STEP_FIELDS = ("agent", "generation", "connected", "quarantined", "reservation", "privileged_seconds", "seq")


def _step(steps, case, step, *cards):
    """One timestamped row of the evidence: the state of each card at a scenario step (agent as a short digest, no owner)."""
    row = {"step": step, "wall": round(time.time(), 3), "epoch": case.client.health()["epoch"]}
    for name in cards:
        card = case.client.device(name)
        view = m40.observed(card)
        row[name] = {"generation": view["generation"], "agent": hashlib.sha256(str(view["agent"]).encode()).hexdigest()[:12],
                     "connected": view["connected"], "quarantined": view["quarantined"], "reserved": view["reservation"] is not None,
                     "privileged_seconds": view["privileged_seconds"], "seq": view["seq"]}
    steps.append(row)
    return row


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    os.chmod(path, 0o600)


def _persist(out, writers):
    """Run EVERY evidence writer independently: one failure is an incident and never stops the others or replaces an error in flight.

    Returns True only when all of them succeeded (the caller refuses a PASS otherwise, but only when no other error is in flight)."""
    complete = True
    for name, write in writers:
        try:
            write()
        except Exception as error:                           # noqa: BLE001 - reported, never raised
            complete = False
            out.incident("infrastructure", f"evidence {name} not written ({type(error).__name__}); the original outcome is unchanged")
    return complete


def _restrict(directory):
    """Dumps and their JSON sidecars of a reservation directory: owner only (the client writes them with the process umask)."""
    for path in directory.glob("flash-*"):
        os.chmod(path, 0o600)


# ------------------------------------------------------------------------------------------------------------------------
def test_m40_12a_simultaneous_reservations_and_marks(bench_client, qual_session, pytestconfig, record_property):
    cfg = qual_session
    require_target(cfg, TARGET_CHIP)
    require_consent(cfg, "M40-12A")
    other, first_id = _second_card_id(cfg), cfg["board_id"]
    manifest = build_manifest(cfg, "M40-12a", regions=[], steps=["No operator gesture; `mark` has no hardware effect"])
    case, out = make_campaign(bench_client, cfg, "M40-12a", __file__, pytestconfig, manifest=manifest), Outcome()
    nonce = uuid.uuid4().hex[:8]
    label = {"a": f"m40-12a:{first_id}:{nonce}", "b": f"m40-12a:{other}:{nonce}", "b2": f"m40-12a:{other}:{nonce}:again"}
    record_property("devices", f"{first_id},{other}")
    steps = []
    with recorded(case, "M40-12a", out, preconditions=f"{cfg['chip']} x2 ({first_id}, {other}); one mark per card", limits=LIMITS):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        compatible, basis, reasons = _server_compat(case, cfg, case.client.health())
        if not compatible:
            raise Blocked("; ".join(reasons))
        first, second = case.client.device(first_id), _second_card(case.client, other)
        problems = m40.check_ready(first, first_id) + m40.check_ready(second, other) + m40.check_distinct(first, second)
        problems += [f"{name} has no `mark` capability" for name, card in ((first_id, first), (other, second)) if "mark" not in card.get("capabilities", [])]
        problems += [f"{other} reports chip {second.get('chip')}, expected {cfg['chip']}"] if second.get("chip") != cfg["chip"] else []
        if problems:
            raise Blocked("; ".join(problems))
        base, epoch = {first_id: first["seq"], other: second["seq"]}, case.epoch
        _step(steps, case, "initial", first_id, other)
        try:
            a = case.reserve(ttl=300)
            b = case.client.reserve(other, ttl=300, output_dir=case.directory / "board-second")
            case.boards.append(b)                              # cleanup and unresolved-outcome detection cover the second reservation too
            generation = {first_id: a.generation, other: b.generation}
            assert generation == {first_id: first["generation"], other: second["generation"]}, "a reservation captured another generation"
            assert a.lease_id != b.lease_id, "both cards share one lease"
            _step(steps, case, "both reserved", first_id, other)
            problems = m40.check_reservations(case.client.device(first_id), case.client.device(other), first_held=True, second_held=True)
            assert not problems, "; ".join(problems)
            cursor_a, cursor_b = a.cursor(), b.cursor()
            a.command("mark", {"label": label["a"]})
            op_a = dict(a.last_operation or {})
            b.command("mark", {"label": label["b"]})
            op_b = dict(b.last_operation or {})
            _step(steps, case, "marks submitted", first_id, other)
            for problems in (m40.check_mark_operation(op_a, device=first_id, generation=generation[first_id], label=label["a"]),
                             m40.check_mark_operation(op_b, device=other, generation=generation[other], label=label["b"])):
                assert not problems, "; ".join(problems)
            events_a, events_b = a.events(cursor_a), b.events(cursor_b)
            for problems in (m40.check_marks(events_a, [label["a"]]), m40.check_marks(events_b, [label["b"]])):
                assert not problems, "; ".join(problems)
            a.close()                                          # release the first card ...
            _step(steps, case, "first released", first_id, other)
            problems = m40.check_reservations(case.client.device(first_id), case.client.device(other), first_held=False, second_held=True)
            assert not problems, "releasing the first card changed the second one: " + "; ".join(problems)
            b.command("mark", {"label": label["b2"]})           # ... the second one is still usable
            op_b2 = dict(b.last_operation or {})
            problems = m40.check_mark_operation(op_b2, device=other, generation=generation[other], label=label["b2"])
            assert not problems, "; ".join(problems)
            assert not m40.check_marks(b.events(cursor_b), [label["b2"]]), "the second mark is missing or foreign on the second card"
            idle = m40.drain_events(case.client, first_id, cursor_a.after, epoch=epoch, fields=("seq", "kind", "detail"))
            assert not m40.check_marks(idle, []) and not [e for e in idle if e["kind"] == "operation"], "the released card received a mark or an operation"
            b.close()
            _step(steps, case, "both released", first_id, other)
        finally:
            _write_jsonl(case.directory / "m40-12a-steps.jsonl", steps)
        # --- final state, whole-run capture per card, no replay -------------------------------------------------------------
        for name in (first_id, other):
            card = case.client.device(name)
            assert (card["connected"], card["quarantined"], card["reservation"], card["privileged_seconds"]) == (True, False, None, 0), \
                f"{name} is not back to idle: {m40.observed(card)}"
            assert card["generation"] == generation[name], f"{name} changed generation"
        capture = {name: m40.drain_events(case.client, name, base[name], epoch=epoch, fields=("seq", "kind", "generation", "detail"))
                   for name in (first_id, other)}
        for name, events in capture.items():
            for problems in (m40.check_event_kinds(events, MARK_KINDS), m40.check_contiguous(events, base[name])):
                assert not problems, f"{name}: " + "; ".join(problems)
        assert not m40.check_marks(capture[first_id], [label["a"]]) and not m40.check_marks(capture[other], [label["b"], label["b2"]])
        assert not m40.check_commands(m40.read_commands(a.output_dir), [("mark", {"label": label["a"]})]), "replay or foreign command on the first card"
        assert not m40.check_commands(m40.read_commands(b.output_dir), [("mark", {"label": label["b"]}), ("mark", {"label": label["b2"]})]), \
            "replay or foreign command on the second card"
        assert case.client.submissions == 3, f"{case.client.submissions} operations submitted, expected 3"
        grew = {name: capture[name][-1]["seq"] - base[name] if capture[name] else 0 for name in capture}
        out.evidence.append(f"both cards reserved together (distinct leases); one mark each, labels echoed, events exactly their own; "
                            f"first released -> second still reserved and usable (second mark); no foreign event, no quarantine, no generation change")
        out.evidence.append(f"event growth (expected, not constant): {first_id} +{grew[first_id]}, {other} +{grew[other]}; kinds "
                            f"{sorted({e['kind'] for e in capture[first_id]})} / {sorted({e['kind'] for e in capture[other]})}; contiguous; exactly 3 submissions")


# ------------------------------------------------------------------------------------------------------------------------
def test_m40_11c_11d_known_content_and_application_return(bench_client, qual_session, pytestconfig, record_property):
    cfg = qual_session
    require_target(cfg, TARGET_CHIP, "external_uart")
    require_consent(cfg, "M40-11C")
    known = cfg["m40"].get("known")
    if not known:
        pytest.skip("BLOCKED: no m40.known section (table offset and the three UART markers) in the private session; none is presumed")
    if known["app_marker"].startswith("REPLACE-"):
        pytest.skip("BLOCKED: m40.known.app_marker is still the template placeholder; declare a line the application prints at every boot")
    other, first_id = _second_card_id(cfg), cfg["board_id"]
    manifest = build_manifest(cfg, "M40-11cd", regions=[], steps=["No operator gesture; the first card restarts on loader entry and console return"])
    case, out = make_campaign(bench_client, cfg, "M40-11cd", __file__, pytestconfig, manifest=manifest), Outcome()
    record_property("target", first_id)
    record_property("observed", other)
    record_property("table_offset", hex(known["table_offset"]))
    token = os.environ["BENCH_CLIENT_TOKEN"]
    with recorded(case, "M40-11cd", out, preconditions=f"{cfg['chip']} / external_uart; table {known['table_offset']:#x}; {other} observed",
                  limits=LIMITS + "; the markers prove the application printed its declared line after the console return, not that it is correct"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        compatible, basis, reasons = _server_compat(case, cfg, case.client.health())
        if not compatible:
            raise Blocked("; ".join(reasons))
        second = _second_card(case.client, other)
        problems = m40.check_ready(second, other) + m40.check_distinct(case.client.device(first_id), second)
        if problems:
            raise Blocked("; ".join(problems))
        watch = m40.BoardWatch(case.client.url, token, other, interval=cfg["m40"]["sample_interval_s"])
        case.write("m40-baseline-observed-card.json", watch.start())
        planned, reads_log, c_problems, d_problems, result = [], [], [], [], None
        located, transport_details, issued, facts, valid_headers = [], [], False, {}, 0
        at_by_seq, state = {}, {"phase": "reserve", "error": None, "persisted": False}
        try:
            board = case.reserve(ttl=600)
            generation = board.generation
            state["phase"] = "bootloader-header"
            try:
                with retained_reads(board, out) as reads:
                    def read(address, size):
                        planned.append(("flash.read", {"address": address, "size": size}))
                        data = reads.read(address=address, size=size, timeout=cfg["operation_timeout_s"])
                        reads_log.append({"address": address, "size": size, "sha256": hashlib.sha256(data).hexdigest(), "erased": m40.is_erased(data)})
                        return data

                    def twice(address, size):
                        first, again = read(address, size), read(address, size)
                        if first != again:
                            c_problems.append(f"two reads of {address:#x}+{size:#x} differ")
                        return first
                    # --- 11c: known content --------------------------------------------------------------------------------------
                    header = twice(0, 4096)
                    facts, problems = m40.check_image_header(header, cfg["chip"])
                    c_problems += [f"bootloader header at 0: {p}" for p in problems]
                    state["phase"] = "table"
                    planned.append(("partitions", {"address": known["table_offset"]}))
                    valid, probes = probe_candidates(board, [known["table_offset"]])
                    case.write("m40-11c-table-probe.json", probes)
                    if len(valid) != 1:
                        raise Blocked(f"no valid partition table at the declared offset {known['table_offset']:#x}; none is presumed")
                    address, rows = valid[0]
                    raw = twice(address, 4096)
                    c_problems += m40.check_table_agreement(rows, decode_table(raw))
                    check_rows(rows, cfg["flash_size"])
                    state["phase"] = "application-slots"
                    slots = m40.app_partitions(rows)
                    if not slots:
                        c_problems.append("the table has no application partition")
                    for slot in slots[:4]:
                        head = twice(slot["address"], 4096)
                        slot_facts, slot_problems = m40.check_image_header(head, cfg["chip"])
                        if not slot_problems:
                            valid_headers += 1
                        elif not m40.is_erased(head):
                            c_problems.append(f"slot {slot['label']}: neither a valid image header nor erased ({'; '.join(slot_problems)})")
                    if slots and not valid_headers:
                        c_problems.append("no application slot holds a valid image header (erased slots prove nothing about addressing)")
                    # --- 11d: console return, then the three markers IN ORDER ---------------------------------------------------
                    cursor = board.cursor()                        # BEFORE the command that restarts the application
                    planned.append(("console", None))
                    issued = True
                    state["phase"] = "console"
                    board.console()
                    state["phase"] = "markers"
                    chunks, deadline = [], time.monotonic() + known["marker_timeout_s"]
                    wanted = [("rom", known["rom_marker"].encode()), ("bootloader", known["bootloader_marker"].encode()),
                              ("application", known["app_marker"].encode())]
                    while True:
                        new = board.events(cursor)                 # ContinuityError (INDETERMINATE) on disconnect / new generation / UART loss
                        chunks += m40.decode_chunks(new)
                        at_by_seq.update({e["seq"]: e.get("at") for e in new})
                        transport_details += [e.get("detail", "") for e in new if e["kind"] == "transport"]
                        located = m40.locate_markers(chunks, wanted)
                        if all(item["found"] for item in located) or time.monotonic() >= deadline:
                            break
                        time.sleep(0.2)
                    d_problems = m40.marker_problems(located)
                    if not any("console opened" in detail for detail in transport_details):
                        d_problems.append("no `console opened` transport event after the console command")
            except BaseException as error:
                state["error"] = type(error).__name__
                if not issued:
                    _console_after_early_exit(board, out, error)   # the card may still be in the loader
                elif state["phase"] == "console":
                    # The console command itself did not complete cleanly: never retried, the card may still be in the loader.
                    out.restoration = "UNKNOWN: the console return did not complete; the card may be left in the ROM loader - local recovery required"
                    out.incident("bench", "console command did not complete: no retry, no second console")
                raise
        finally:
            # Nothing in this block may raise: the error in flight (if any) stays the verdict, and every piece of evidence still available is kept.
            try:
                result = watch.stop()
            except Exception as error:                         # noqa: BLE001 - a collector failure is an incident AND an incomplete observation
                result = watch.result
                result.errors.append(f"collector stop failed ({type(error).__name__})")
                out.incident("infrastructure", f"collector stop failed ({type(error).__name__}); the observation of the second card is incomplete")
            first_dir = case.directory / "board-0"
            state["persisted"] = _persist(out, [
                ("board-0 file permissions", lambda: _restrict(first_dir) if first_dir.is_dir() else None),          # privacy first
                ("m40-observed-card.jsonl", lambda: m40.write_evidence(case.directory / "m40-observed-card.jsonl", result)),
                ("m40-11c-reads.json", lambda: case.write("m40-11c-reads.json", {
                    "phase": state["phase"], "error": state["error"], "reads": reads_log, "header_facts": facts,
                    "problems": c_problems, "planned": [name for name, _ in planned]})),
                ("m40-11d-markers.json", lambda: case.write("m40-11d-markers.json", {
                    "phase": state["phase"], "error": state["error"], "timeout_s": known["marker_timeout_s"],
                    "markers": [{**item, "at": at_by_seq.get(item["seq"])} for item in located],
                    "transport": transport_details, "problems": d_problems}))])
        assert state["persisted"], "the evidence of this run could not be written (no error was in flight: the run cannot be a PASS)"
        # --- no replay, release, continuity, second card ------------------------------------------------------------------------
        board.close()
        problems = m40.check_commands(m40.read_commands(board.output_dir), planned)
        assert not problems, "replay or unexpected command: " + "; ".join(problems)
        assert case.client.submissions == len(planned), f"{case.client.submissions} operations submitted, expected {len(planned)}"
        after = case.client.device(first_id)
        assert after["reservation"] is None and after["generation"] == generation, "the first card is not idle on its own generation"
        events = m40.drain_events(case.client, first_id, case.after, epoch=case.epoch)
        problems = m40.check_continuity(events, generation)
        if problems:
            raise ContinuityError("continuity of the target card interrupted: " + "; ".join(problems))
        incomplete = m40.check_complete(result)
        if incomplete:
            raise ContinuityError("observation of the second card incomplete: " + "; ".join(incomplete))
        problems = m40.check_independent(result)
        assert not problems, "second card: " + "; ".join(problems)
        out.evidence.append(f"M40-11c: {'PASS' if not c_problems else 'FAIL'} - {len(reads_log)} reads (each repeated, equal digests), bootloader header "
                            f"chip id {facts.get('chip_id')}, table at {known['table_offset']:#x} decoded independently = API view, "
                            f"{valid_headers} valid application slot header(s), erased reads counted but never used as proof")
        out.evidence.append(f"M40-11d: {'PASS' if not d_problems else 'FAIL'} - markers in order: " +
                            ", ".join(f"{i['name']}@seq{i['seq']}" if i["found"] else f"{i['name']}=MISSING" for i in located))
        out.evidence.append(f"{other}: {len(result.samples)} GET samples, state unchanged, {m40.spontaneous_uart(result)} spontaneous UART (counted only)")
        assert not c_problems, "M40-11c: " + "; ".join(c_problems)
        assert not d_problems, "M40-11d: " + "; ".join(d_problems)


# ------------------------------------------------------------------------------------------------------------------------
def test_m40_4_simultaneous_uart(bench_client, qual_session, pytestconfig, record_property):
    cfg = qual_session
    require_target(cfg, TARGET_CHIP)
    spec = cfg["m40"].get("uart")
    if not spec:
        pytest.skip("BLOCKED: no m40.uart section (window, thresholds, distinctive markers per card) in the private session; none is presumed")
    if any(m.startswith("REPLACE-") for markers in spec["markers"].values() for m in markers):
        pytest.skip("BLOCKED: m40.uart.markers still holds template placeholders; declare each card's own distinctive lines")
    if spec["stimulus"] == "reset_both":
        require_consent(cfg, "M40-4-RESET")
        if not pytestconfig.getoption("--qual-allow-reset", default=False):
            pytest.skip("BLOCKED: stimulus reset_both also needs --qual-allow-reset (a reset of the second card is never implied)")
    other, first_id = _second_card_id(cfg), cfg["board_id"]
    manifest = build_manifest(cfg, "M40-4", regions=[], steps=["No operator gesture"])
    case, out = make_campaign(bench_client, cfg, "M40-4", __file__, pytestconfig, manifest=manifest), Outcome()
    token = os.environ["BENCH_CLIENT_TOKEN"]
    for key in ("target", "observed", "stimulus"):
        record_property(key, {"target": first_id, "observed": other, "stimulus": spec["stimulus"]}[key])
    own = {name: [m.encode() for m in markers] for name, markers in spec["markers"].items()}
    with recorded(case, "M40-4", out, preconditions=f"{cfg['chip']} x2 ({first_id}, {other}); stimulus {spec['stimulus']}; window {spec['window_s']} s",
                  limits=LIMITS_UART):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        compatible, basis, reasons = _server_compat(case, cfg, case.client.health())
        if not compatible:
            raise Blocked("; ".join(reasons))
        first, second = case.client.device(first_id), _second_card(case.client, other)
        problems = m40.check_ready(first, first_id) + m40.check_ready(second, other) + m40.check_distinct(first, second)
        if spec["stimulus"] == "reset_both":
            problems += [f"{name} has no `reset` capability" for name, card in ((first_id, first), (other, second)) if "reset" not in card.get("capabilities", [])]
        if problems:
            raise Blocked("; ".join(problems))
        base, epoch = {first_id: first["seq"], other: second["seq"]}, case.epoch
        generations = {first_id: first["generation"], other: second["generation"]}
        watches, steps = {}, []
        _step(steps, case, "initial", first_id, other)
        try:
            for name in (first_id, other):                     # GET-only observation of BOTH cards from before anything happens, in both modes
                watches[name] = m40.BoardWatch(case.client.url, token, name, interval=cfg["m40"]["sample_interval_s"])
                case.write(f"m40-4-baseline-{'a' if name == first_id else 'b'}.json", watches[name].start())
            if spec["stimulus"] == "none":
                time.sleep(spec["window_s"])
            else:
                a = case.reserve(ttl=300)
                b = case.independent_client(token).reserve(other, ttl=300, output_dir=case.directory / "board-second")
                case.boards.append(b)
                _step(steps, case, "both reserved", first_id, other)
                timeout, start_timeout = cfg["operation_timeout_s"], 30
                fired = m40.fire_concurrently({first_id: a, other: b}, "reset", timeout=timeout, start_timeout=start_timeout,
                                              join_timeout=start_timeout + timeout + case.client.request_timeout + 5)
                _step(steps, case, "resets settled or abandoned", first_id, other)
                if fired.alive:
                    # The command may still be running: its outcome is UNKNOWN. No release, no second submission: Campaign.close() keeps the
                    # reservation of any board whose operation is not settled, and the verdict is INDETERMINATE.
                    raise PendingOperation(f"reset still running on {fired.alive} after the join deadline: outcome unknown, reservations retained, no replay")
                for name in (first_id, other):
                    if name in fired.errors:
                        raise fired.errors[name]               # PendingOperation / ContinuityError -> INDETERMINATE, anything else -> FAIL
                time.sleep(spec["window_s"])
                a.close()
                b.close()
                _step(steps, case, "both released", first_id, other)
        finally:
            for name, watch in watches.items():
                result = watch.stop()
                m40.write_evidence(case.directory / f"m40-observed-{'a' if name == first_id else 'b'}.jsonl", result)
                watches[name] = result
            _write_jsonl(case.directory / "m40-4-steps.jsonl", steps)
        fields = ("seq", "kind", "generation", "source_lost", "source_seq", "at", "data")
        streams = {name: m40.drain_events(case.client, name, base[name], epoch=epoch, fields=fields) for name in (first_id, other)}
        activity = {name: m40.uart_activity(events) for name, events in streams.items()}
        overlap = m40.window_overlap(activity[first_id], activity[other])
        alternations = m40.interleavings(streams[first_id], streams[other])
        data = {name: b"".join(chunk for _, chunk in m40.decode_chunks(events)) for name, events in streams.items()}
        evidence = {"activity": activity, "overlap_s": round(overlap, 3), "interleavings": alternations,
                    "own_markers_received": {name: [marker in data[name] for marker in own[name]] for name in streams}}
        case.write("m40-4-activity.json", evidence)
        if spec["stimulus"] == "none":
            silent = [name for name, item in activity.items() if item["bytes"] < spec["min_bytes"]]
            if silent:
                raise Blocked(f"no UART source on {silent} during the {spec['window_s']} s window (the bench does not provide the source); "
                              "declare a stimulus or run when the firmwares print")
        for name, result in watches.items():
            incomplete = m40.check_complete(result)             # epoch on every page, lost / duplicate / missing events, thread terminated, final sample
            if incomplete:
                raise ContinuityError(f"observation of {name} incomplete: " + "; ".join(incomplete))
            if spec["stimulus"] == "none":                     # passive: the cards must not change AT ALL (reservations included)
                problems = m40.check_independent(result)
                assert not problems, f"{name}: " + "; ".join(problems)
            else:                                              # active: reservations and operation events are expected, identity must hold
                continuity = m40.stimulated_continuity(result)
                if continuity:
                    raise ContinuityError(f"{name}: " + "; ".join(continuity))
                problems = m40.stimulated_violations(result)
                assert not problems, f"{name}: " + "; ".join(problems)
        # --- identity, state, release ----------------------------------------------------------------------------------------
        for name in (first_id, other):
            card = case.client.device(name)
            if card["generation"] != generations[name] or not card["connected"]:
                raise ContinuityError(f"{name} changed generation or disconnected during the run (USB re-enumeration?): continuity lost")
            assert (card["quarantined"], card["reservation"], card["privileged_seconds"]) == (False, None, 0), f"{name} is not idle: {m40.observed(card)}"
        # --- the criteria ----------------------------------------------------------------------------------------------------------
        problems = []
        for name, item in activity.items():
            if item["bytes"] < spec["min_bytes"]:
                problems.append(f"{name}: {item['bytes']} bytes received, at least {spec['min_bytes']} required")
            problems += [f"{name}: {p}" for p in m40.check_uart_continuity(streams[name])]
        problems += m40.check_uart_isolation(streams, own)
        if overlap < spec["min_overlap_s"]:
            problems.append(f"UART windows overlap for {overlap:.2f} s, at least {spec['min_overlap_s']} s required")
        if alternations < spec["min_interleavings"]:
            problems.append(f"arrival order alternates {alternations} times, at least {spec['min_interleavings']} required")
        out.evidence.append(f"{first_id}: {activity[first_id]['bytes']} B in {activity[first_id]['events']} events; {other}: {activity[other]['bytes']} B in "
                            f"{activity[other]['events']} events; windows overlap {overlap:.2f} s; arrival order alternates {alternations} times")
        out.evidence.append("each card's own markers received, none of the other's; seq / source_seq continuous; generations, agents, reservations unchanged; "
                            f"stimulus: {spec['stimulus']}")
        assert not problems, "; ".join(problems)

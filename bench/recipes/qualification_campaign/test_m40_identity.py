"""M40-ID and M40-PHYS-B (issue #40): hardware identity of the two cards and their association with a physical cable.

Same architecture as the other M40 recipes: pytest -> official client -> REST API; the browser keeps the ports. No write, no erase, no grant,
no server or protocol change. Nothing is implied by --bench: every scenario needs its own consent in the PRIVATE session.

M40-ID  (consent M40-ID; M40-ID-ENROLL only to record a first reference)
  Reserves both cards, runs TWO independent `identify` per card (interleaved first, second, first, second: four commands, no `return_console`
  argument exists for identify), reduces every raw result to a fingerprint (identity.py: HMAC-SHA-256 over the canonical form
  iobewi-identity-v1|chip|mac|flash_size|flash_id, 32-byte private key) and clears the raw result at once.
  Hardware transitions: each identify enters the ROM loader and returns to the console = 2 resets; 4 identify = 8 transitions (4 per card).
  PASS           per card the two reads agree on chip, MAC, flash_size and flash_id; the two cards have different digests and MACs; each identify is
                 followed by `opening loader` and `console opened` on ITS card and by nothing foreign; generations, agents and epoch never change;
                 exactly [identify, identify] per card and four submissions (no replay); reservations are released when the state is safe; when a
                 reference exists both cards equal it (compared, never replaced); when none exists and M40-ID-ENROLL is granted the reference is
                 recorded once, atomically, after every other check;
  FAIL           a field changes between reads, the cards are not distinct, a foreign event, a differing reference, an enrolment that cannot be
                 recorded, or a result without a usable identity;
  BLOCKED        no consent / no m40.identity / no key (and no create_key) / an unusable reference (never repaired) / a card not ready /
                 --showlocals (it would print raw identities);
  INDETERMINATE  an unresolved operation, a lost proof window (generation, epoch, events), or a card whose console return is NOT proven: it is
                 kept reserved (never released on an unknown state) and nothing is replayed.
M40-PHYS-B  (consent M40-PHYS-B for the cable gesture; M40-PHYS-B-ID additionally for ONE identify of B after its return)
  Observes both cards by GET, asks the operator to unplug and replug the cable of B only, and proves from the server's events that B went away
  and came back under a NEW generation while A did not move. That is a PHYSICAL ASSOCIATION (logical id <-> cable); it says nothing about the
  hardware identity. With M40-PHYS-B-ID, B is reserved after its return, identified once and its fingerprint compared with the reference
  (never replacing it): equality ties the logical id, the cable AND the hardware. A new agent generation is NOT a new physical identity.
  Hardware transitions: the operator's replug (the page re-opens the port and re-identifies B: 2 resets, not controlled by the recipe) and, with
  M40-PHYS-B-ID, 2 more for the identify. A gesture that demonstrably hits A first (A lost while B was not), a quarantine / grant / foreign event on A, or a different identity is a
  FAIL; any other loss of continuity of A (it re-registers, its generation changes) is INDETERMINATE because its cause cannot be attributed to the
  gesture; B never coming back is INDETERMINATE; a gesture never seen is BLOCKED.
Evidence (fingerprints, timeline, steps) is written in a `finally`, independently file by file, with 0600 permissions, in every verdict. It holds
digests, the last two octets of the MAC (diagnostics only) and no raw MAC and no key. Hardware status: NOT executed.
"""
import contextlib
import os
import time
from pathlib import Path

import pytest

from iobewi_bench.client import ContinuityError, PendingOperation

from . import identity, m40
from .harness import Blocked, Outcome, make_campaign, recorded
from .readonly import record_transport, require_consent, require_target, start_or_block
from .session import build_manifest
from .test_m40_complement import _persist, _step, _write_jsonl
from .test_m40_multiboard import LIMITS, TARGET_CHIP, _second_card, _second_card_id, _server_compat

pytestmark = pytest.mark.bench
LIMITS_ID = (LIMITS + "; the raw MAC exists in the page agent, in the server's memory and in the HTTP response of identify (the durable journal "
             "strips it); a fingerprint proves that the SAME hardware answered, not which cable it sits on")
IDENTIFY_KINDS = {"operation", "transport", "uart"}
PHYS_B_KINDS = {"disconnected", "connected", "operation", "transport", "uart"}


def _identity_spec(cfg, pytestconfig):
    spec = cfg["m40"].get("identity")
    if not spec:
        pytest.skip("BLOCKED: no m40.identity section (key file and reference file) in the private session; none is presumed")
    if pytestconfig.getoption("showlocals", default=False):
        pytest.skip("BLOCKED: --showlocals would print raw identities in tracebacks; run without it")
    return spec


def _load_key(spec, out):
    path = Path(spec["key_file"])
    if not path.exists() and not path.is_symlink():
        if not spec["create_key"]:
            raise Blocked("no identity key and m40.identity.create_key is not set: a key is never created implicitly")
        try:
            identity.create_key(path)
        except (identity.IdentityError, OSError) as error:
            raise Blocked(f"the identity key could not be created ({type(error).__name__}: {error})") from None
        out.evidence.append("identity key created once (32 random bytes, 0600, outside Git); its content is never printed")
    try:
        return identity.load_key(path)
    except (identity.IdentityError, OSError) as error:
        raise Blocked(f"the identity key is unusable ({type(error).__name__}: {error}); it is never repaired or replaced automatically") from None


def _load_reference(spec, cards):
    path = Path(spec["reference_file"])
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return identity.load_reference(path, cards)
    except (identity.IdentityError, OSError) as error:
        raise Blocked(f"the identity reference is unusable ({type(error).__name__}: {error}); it is never repaired or replaced automatically") from None


def _identify(board, key):
    """ONE identify: fingerprint at once, raw result cleared, transport events of THIS card around it."""
    cursor = board.cursor()
    result = board.identify()
    try:
        fingerprint = identity.fingerprint(result, key)
    finally:
        identity.scrub(result)
    details = [event.get("detail", "") for event in board.events(cursor) if event["kind"] == "transport"]
    return fingerprint, {"loader": any("opening loader" in d for d in details), "console": any("console opened" in d for d in details)}


def _private_json(case, name, value):
    """case.write, then 0600: the identity evidence is owner-only from the moment the run ends (its directory is 0700)."""
    case.write(name, value)
    os.chmod(case.directory / name, 0o600)


def _announce(pytestconfig, case, text, *, timeout):
    """Ask the operator for a gesture, LIVE: the prompt bypasses pytest's output capture (no -s needed) and is recorded in the run directory."""
    case.client.record("operator-requests.jsonl", {"request": text, "timeout_s": timeout})
    capture = pytestconfig.pluginmanager.getplugin("capturemanager")
    with capture.global_and_fixture_disabled() if capture else contextlib.nullcontext():
        print(f"OPERATOR>> {text} (waiting up to {timeout:.0f} s)", flush=True)


def _read_row(round_number, fingerprint, check):
    return {"round": round_number, "digest": fingerprint["digest"], "fields": fingerprint["fields"], "mac_suffix": fingerprint["mac_suffix"], **check}


def _hold_if_console_not_proven(board, name, check):
    if not check["console"]:
        board.uncertain = True                       # Board.close() will refuse to release: the card may still be in the loader
        raise PendingOperation(f"{name}: no `console opened` event after identify; the card may still be in the loader - reservation kept, no replay")


# ------------------------------------------------------------------------------------------------------------------------
def test_m40_id_hardware_identity(bench_client, qual_session, pytestconfig, record_property):
    cfg = qual_session
    require_target(cfg, TARGET_CHIP)
    require_consent(cfg, "M40-ID")
    spec = _identity_spec(cfg, pytestconfig)
    other, first_id = _second_card_id(cfg), cfg["board_id"]
    enroll = "M40-ID-ENROLL" in cfg["consent"]["scenarios"]
    manifest = build_manifest(cfg, "M40-ID", regions=[], steps=["No operator gesture; each identify enters the loader and returns to the console"])
    case, out = make_campaign(bench_client, cfg, "M40-ID", __file__, pytestconfig, manifest=manifest), Outcome()
    record_property("devices", f"{first_id},{other}")
    record_property("enroll_consent", str(enroll).lower())
    reads, steps = {first_id: [], other: []}, []
    state = {"phase": "start", "error": None, "persisted": False}
    with recorded(case, "M40-ID", out, preconditions=f"{cfg['chip']} x2 ({first_id}, {other}); 2 identify each; 8 loader/console transitions", limits=LIMITS_ID):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        compatible, basis, reasons = _server_compat(case, cfg, case.client.health())
        if not compatible:
            raise Blocked("; ".join(reasons))
        key = _load_key(spec, out)
        reference = _load_reference(spec, [first_id, other])
        first, second = case.client.device(first_id), _second_card(case.client, other)
        problems = m40.check_ready(first, first_id) + m40.check_ready(second, other) + m40.check_distinct(first, second)
        problems += [f"{name} has no `identify` capability" for name, card in ((first_id, first), (other, second)) if "identify" not in card.get("capabilities", [])]
        problems += [f"{other} reports chip {second.get('chip')}, expected {cfg['chip']}"] if second.get("chip") != cfg["chip"] else []
        if problems:
            raise Blocked("; ".join(problems))
        base, epoch = {first_id: first["seq"], other: second["seq"]}, case.epoch
        generations = {first_id: first["generation"], other: second["generation"]}
        checks, boards = {first_id: [], other: []}, {}
        _step(steps, case, "initial", first_id, other)
        try:
            state["phase"] = "reserve"
            boards = {first_id: case.reserve(ttl=300), other: case.client.reserve(other, ttl=300, output_dir=case.directory / "board-second")}
            case.boards.append(boards[other])               # cleanup and unresolved-outcome detection cover the second reservation
            assert boards[first_id].lease_id != boards[other].lease_id, "both cards share one lease"
            assert (boards[first_id].generation, boards[other].generation) == (generations[first_id], generations[other]), "a reservation captured another generation"
            _step(steps, case, "both reserved", first_id, other)
            for round_number in (1, 2):
                for name in (first_id, other):
                    state["phase"] = f"identify-{round_number}-{name}"
                    fingerprint, check = _identify(boards[name], key)
                    reads[name].append(fingerprint)
                    checks[name].append(check)
                    _step(steps, case, f"identify {round_number} {name}", first_id, other)
                    _hold_if_console_not_proven(boards[name], name, check)
            state["phase"] = "checks"
        except BaseException as error:
            state["error"] = type(error).__name__
            raise
        finally:
            state["persisted"] = _persist(out, [
                ("m40-id-fingerprints.json", lambda: _private_json(case, "m40-id-fingerprints.json", {
                    "phase": state["phase"], "error": state["error"], "canonical": identity.CANONICAL_VERSION,
                    "reads": {name: [_read_row(i + 1, fp, checks[name][i] if i < len(checks[name]) else {}) for i, fp in enumerate(items)] for name, items in reads.items()}})),
                ("m40-id-steps.jsonl", lambda: _write_jsonl(case.directory / "m40-id-steps.jsonl", steps))])
        # --- the criteria ------------------------------------------------------------------------------------------------------
        problems = identity.check_stable(reads[first_id], first_id) + identity.check_stable(reads[other], other)
        problems += identity.check_distinct(reads[first_id][0], reads[other][0], (first_id, other))
        assert not problems, "; ".join(problems)
        capture = {name: m40.drain_events(case.client, name, base[name], epoch=epoch, fields=("seq", "kind", "generation", "detail")) for name in (first_id, other)}
        for name, events in capture.items():
            problems = m40.check_event_kinds(events, IDENTIFY_KINDS) + m40.check_contiguous(events, base[name])
            details = [e.get("detail") or "" for e in events if e["kind"] == "transport"]
            if sum("opening loader" in d for d in details) != 2 or sum("console opened" in d for d in details) != 2:
                problems.append(f"expected exactly 2 loader entries and 2 console returns, saw {details!r}")
            if sum(1 for e in events if e["kind"] == "operation") != 2:
                problems.append("expected exactly 2 operation events")
            assert not problems, f"{name}: " + "; ".join(problems)
        for name in (first_id, other):
            assert not m40.check_commands(m40.read_commands(boards[name].output_dir), [("identify", None), ("identify", None)]), f"{name}: replay or foreign command"
        assert case.client.submissions == 4, f"{case.client.submissions} operations submitted, expected 4 (2 identify per card)"
        for name in (first_id, other):
            boards[name].close()
            card = case.client.device(name)
            assert (card["connected"], card["quarantined"], card["reservation"], card["privileged_seconds"]) == (True, False, None, 0), f"{name} is not back to idle"
            assert card["generation"] == generations[name], f"{name} changed generation"
        # A persistence failure never hides a real failure above (those assertions came first), and it blocks the enrolment below.
        assert state["persisted"], "the evidence of this run could not be written (no error was in flight: the run cannot be a PASS)"
        current = {name: reads[name][0] for name in (first_id, other)}
        if reference is not None:
            problems = identity.compare_reference(reference, current)
            assert not problems, "; ".join(problems)
            reference_mode = "compared with the recorded reference (not replaced)"
        elif enroll:
            try:
                identity.write_reference(spec["reference_file"], identity.reference_document(current))
            except identity.ReferenceExists:
                raise AssertionError("a reference appeared during the run: it is never overwritten") from None
            except Exception as error:                      # noqa: BLE001 - nothing partial is ever a valid reference
                raise AssertionError(f"the reference could not be recorded ({type(error).__name__}): no valid reference exists") from None
            reference_mode = "no reference existed: recorded ONCE after every other check (atomic, 0600, outside the campaign results)"
        else:
            reference_mode = "no reference exists and M40-ID-ENROLL was not granted: nothing recorded, cross-session stability NOT shown"
        out.evidence.append(f"two independent identify per card agree on chip, MAC, flash_size and flash_id; {first_id} {reads[first_id][0]['digest'][:12]} != "
                            f"{other} {reads[other][0]['digest'][:12]} (HMAC-SHA-256, canonical v1); each identify was followed by `opening loader` and `console opened`")
        out.evidence.append(f"flash: {first_id} {reads[first_id][0]['fields']['flash_size']} B id {reads[first_id][0]['fields']['flash_id']}, "
                            f"{other} {reads[other][0]['fields']['flash_size']} B id {reads[other][0]['fields']['flash_id']}; MAC suffixes (diagnostics) "
                            f"{reads[first_id][0]['mac_suffix']} / {reads[other][0]['mac_suffix']}")
        out.evidence.append(f"exactly 4 identify submitted (no replay), 8 loader/console transitions, reservations released, generations and epoch unchanged; reference: {reference_mode}")


# ------------------------------------------------------------------------------------------------------------------------
def test_m40_phys_b_cable_association(bench_client, qual_session, pytestconfig, record_property):
    cfg = qual_session
    require_target(cfg, TARGET_CHIP)
    require_consent(cfg, "M40-PHYS-B")
    phys = cfg["m40"].get("phys_b")
    if not phys:
        pytest.skip("BLOCKED: no m40.phys_b section (gesture timeouts) in the private session; none is presumed")
    with_identity = "M40-PHYS-B-ID" in cfg["consent"]["scenarios"]
    spec = _identity_spec(cfg, pytestconfig) if with_identity else None
    other, first_id = _second_card_id(cfg), cfg["board_id"]
    manifest = build_manifest(cfg, "M40-PHYS-B", regions=[], steps=[f"The operator unplugs and replugs the cable of {other} only"])
    case, out = make_campaign(bench_client, cfg, "M40-PHYS-B", __file__, pytestconfig, manifest=manifest), Outcome()
    record_property("devices", f"{first_id},{other}")
    record_property("identity_option", str(with_identity).lower())
    token = os.environ["BENCH_CLIENT_TOKEN"]
    steps, timeline, fingerprint_rows = [], [], []
    state = {"phase": "start", "error": None, "persisted": False}
    with recorded(case, "M40-PHYS-B", out, preconditions=f"{first_id} stays, the cable of {other} is unplugged and replugged by the operator"
                  + ("; one identify of B after its return" if with_identity else ""), limits=LIMITS_ID):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        compatible, basis, reasons = _server_compat(case, cfg, case.client.health())
        if not compatible:
            raise Blocked("; ".join(reasons))
        key = reference = None
        if with_identity:
            key = _load_key(spec, out)
            reference = _load_reference(spec, [first_id, other])
            if reference is None:
                raise Blocked("M40-PHYS-B-ID needs the reference recorded by M40-ID: none exists, and it is never recorded from this run")
        first, second = case.client.device(first_id), _second_card(case.client, other)
        problems = m40.check_ready(first, first_id) + m40.check_ready(second, other) + m40.check_distinct(first, second)
        if with_identity and "identify" not in second.get("capabilities", []):
            problems.append(f"{other} has no `identify` capability")
        if problems:
            raise Blocked("; ".join(problems))
        base, epoch = {first_id: first["seq"], other: second["seq"]}, case.epoch
        generation_b = second["generation"]
        watch = m40.BoardWatch(case.client.url, token, first_id, interval=cfg["m40"]["sample_interval_s"])
        case.write("m40-baseline-observed-card.json", watch.start())
        result, board_b, events_b = None, None, []
        try:
            _step(steps, case, "initial", first_id, other)
            state["phase"] = "gesture"
            _announce(pytestconfig, case, f"Unplug the USB cable of {other} ONLY, wait a moment, then plug it back (do not touch {first_id})",
                      timeout=phys["disconnect_timeout_s"] + phys["return_timeout_s"])

            def watch_events(until, timeout):
                deadline = time.monotonic() + timeout
                while True:
                    a_events = m40.drain_events(case.client, first_id, base[first_id], epoch=epoch, fields=("seq", "kind", "generation"))
                    found = m40.drain_events(case.client, other, base[other], epoch=epoch, fields=("seq", "kind", "generation", "at"))
                    if any(e["kind"] in ("disconnected", "connected") for e in a_events):
                        if lost(found):                         # B had already gone: the cause of A's change cannot be attributed to the gesture
                            raise ContinuityError(f"{first_id} re-registered during the gesture on {other}: continuity lost, not attributable")
                        case.interrupt_window(f"the gesture hit {first_id} instead of {other}")      # the proof window of A is ended explicitly
                        raise AssertionError(f"{first_id} was disconnected while {other} was not: the gesture hit the wrong cable")
                    if until(found):
                        return found
                    if time.monotonic() >= deadline:
                        return None
                    time.sleep(0.5)

            def lost(found):
                return any(e["kind"] == "disconnected" for e in found)

            def returned(found):
                gone = next((i for i, e in enumerate(found) if e["kind"] == "disconnected"), None)
                return gone is not None and any(e["kind"] == "connected" and e.get("generation") != generation_b for e in found[gone:])
            events_b = watch_events(lost, phys["disconnect_timeout_s"]) or []
            if not events_b or not lost(events_b):
                raise Blocked(f"{other} was not seen disconnecting within {phys['disconnect_timeout_s']:.0f} s: the gesture was not made or not seen")
            _step(steps, case, f"{other} lost", first_id, other)
            state["phase"] = "return"
            events_b = watch_events(returned, phys["return_timeout_s"])
            if events_b is None or not returned(events_b):
                raise ContinuityError(f"{other} did not come back under a new generation within {phys['return_timeout_s']:.0f} s: its state is unknown, nothing is replayed")
            time.sleep(phys["settle_s"])
            events_b = m40.drain_events(case.client, other, base[other], epoch=epoch, fields=("seq", "kind", "generation", "at"))
            _step(steps, case, f"{other} returned", first_id, other)
            timeline = [{"seq": e["seq"], "kind": e["kind"], "at": round(e["at"], 3) if e.get("at") else None, "generation": str(e.get("generation"))[:8]} for e in events_b]
            if with_identity:
                state["phase"] = "identify"
                board_b = case.client.reserve(other, ttl=300, output_dir=case.directory / "board-second")
                case.boards.append(board_b)
                fp, check = _identify(board_b, key)
                fingerprint_rows.append(_read_row(1, fp, check))
                _hold_if_console_not_proven(board_b, other, check)
                _step(steps, case, f"{other} identified", first_id, other)
                state["phase"] = "compare"
                problems = identity.compare_reference(reference, {other: fp})
                assert not problems, "another hardware answered under the logical id (physical association broken): " + "; ".join(problems)
            state["phase"] = "checks"
        except BaseException as error:
            state["error"] = type(error).__name__
            raise
        finally:
            try:
                result = watch.stop()
            except Exception as error:                      # noqa: BLE001 - an incident, never a replacement of the error in flight
                result = watch.result
                result.errors.append(f"collector stop failed ({type(error).__name__})")
                out.incident("infrastructure", f"collector stop failed ({type(error).__name__}); the observation of {first_id} is incomplete")
            state["persisted"] = _persist(out, [
                ("m40-observed-card.jsonl", lambda: m40.write_evidence(case.directory / "m40-observed-card.jsonl", result)),
                ("m40-phys-b-timeline.json", lambda: _private_json(case, "m40-phys-b-timeline.json", {
                    "phase": state["phase"], "error": state["error"], "operator_gesture": "unplug and replug the cable of the logical id under test",
                    "events_of_the_replugged_card": timeline})),
                ("m40-phys-b-fingerprints.json", lambda: _private_json(case, "m40-phys-b-fingerprints.json", {"canonical": identity.CANONICAL_VERSION, "reads": fingerprint_rows})),
                ("m40-phys-b-steps.jsonl", lambda: _write_jsonl(case.directory / "m40-phys-b-steps.jsonl", steps))])
        # --- the criteria ------------------------------------------------------------------------------------------------------
        if board_b is not None:
            board_b.close()
        incomplete = m40.check_complete(result)
        if incomplete:
            raise ContinuityError(f"observation of {first_id} incomplete: " + "; ".join(incomplete))
        continuity = m40.stimulated_continuity(result)
        if continuity:
            raise ContinuityError(f"{first_id} lost its continuity during the gesture on {other} (not attributable to the gesture): " + "; ".join(continuity))
        problems = m40.check_independent(result)
        assert not problems, f"{first_id} was perturbed during the gesture on {other}: " + "; ".join(problems)
        problems = m40.check_event_kinds(events_b, PHYS_B_KINDS)
        assert not problems, f"{other}: " + "; ".join(problems)
        card_b, card_a = case.client.device(other), case.client.device(first_id)
        assert card_b["generation"] != generation_b, f"{other} came back under the SAME generation: no re-registration was proven"
        assert (card_b["connected"], card_b["quarantined"], card_b["reservation"], card_b["privileged_seconds"]) == (True, False, None, 0), f"{other} is not back to idle"
        assert (card_a["generation"], card_a["connected"]) == (first["generation"], True), f"{first_id} changed during the gesture"
        assert state["persisted"], "the evidence of this run could not be written (no error was in flight: the run cannot be a PASS)"
        out.evidence.append(f"PHYSICAL ASSOCIATION: after the operator's gesture on the cable of {other}, {other} was lost then re-registered under a NEW generation "
                            f"({generation_b[:8]} -> {card_b['generation'][:8]}) while {first_id} stayed connected on {first['generation'][:8]} "
                            f"({len(result.samples)} GET samples, no change, {m40.spontaneous_uart(result)} spontaneous UART)")
        if with_identity:
            out.evidence.append(f"HARDWARE IDENTITY (separate from the association): one identify of {other} after its return equals the reference "
                                f"({fingerprint_rows[0]['digest'][:12]}), console returned; the reference was not replaced")
        else:
            out.evidence.append("hardware identity NOT checked (M40-PHYS-B-ID not granted): a new agent generation is not a proof of the same physical card")

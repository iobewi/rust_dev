"""T7 - UART trigger fired by a controlled stimulus (issue #2, requirement 7). Needs a LOCAL operator grant (the `trigger` command is privileged).

The stimulus is the card's own ROM banner after an explicit `reset`: the trigger pattern is the banner marker, the trigger action is a
DEFERRED `reset`. Nothing is written to flash. The card is restarted on purpose (T7 consent).

Criteria, written before any run (both cases inside ONE grant):
  fire_once        arm(pattern=banner, action=reset, delay D); reset -> banner #1; the trigger fires exactly once ("trigger" event after
                   banner #1), the deferred reset produces banner #2 no sooner than 0.8*D after banner #1, and NO third banner appears in the
                   remaining observation time (one-shot);
  rearm_cancels    arm(delay D'); reset -> banner #1 and its trigger event; re-arm with an inert pattern BEFORE D' elapsed: the pending deferred
                   reset is cancelled, so no second banner appears during the observation time and no second trigger event exists;
  PASS             both cases as specified, no operation left unresolved, the grant revoked locally at the end, every observed event
                   corroborated by the server event log (events.jsonl) at comparison time;
  FAIL             a different count (a missing, a duplicated or an unexpected banner / trigger event);
  INDETERMINATE    continuity broke (re-enumeration, disconnection) or an operation is unresolved: no replay;
  BLOCKED          no consent / no reset capability / grant not observed.
Locking and concurrency of the deferred reset with other commands (it never fires while the engine is busy or in the loader) stay covered by the
engine tests only: observing "nothing happened" in the loader is not possible from the UART. Hardware status: NOT executed.
"""
import base64
import time

import pytest

from .harness import Blocked, Outcome, make_campaign, operator_gesture, recorded, wait_until
from .readonly import record_transport, require_consent, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench
DELAY_A, DELAY_B, OBSERVE_A, OBSERVE_B = 1500, 4000, 8.0, 8.0
ASK_GRANT, MIN_GRANT = 300, 120


def observe(board, cursor, marker, seconds):
    """Poll the event stream for `seconds`; return banner events (seq, at) and trigger events (seq, at). Marker split across chunks is handled."""
    banners, triggers, tail = [], [], b""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for event in board.events(cursor):
            if event["kind"] == "trigger":
                triggers.append({"seq": event["seq"], "at": event.get("at")})
            elif event["kind"] == "uart":
                data = tail + base64.b64decode(event["data"])
                for _ in range(data.count(marker)):
                    banners.append({"seq": event["seq"], "at": event.get("at")})
                tail = data[-(len(marker) - 1):] if len(marker) > 1 else b""
        time.sleep(0.1)
    return banners, triggers


def arm(board, pattern, delay):
    return board.command("trigger", {"data": base64.b64encode(pattern).decode(), "action": "reset", "delay_ms": delay, "expires_ms": 90000})


def test_t7_trigger_fires_once_and_rearm_cancels_a_pending_action(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    require_consent(cfg, "T7")
    marker = cfg["readonly"]["reset"]["marker"].encode()
    manifest = build_manifest(cfg, "T7", regions=[], steps=[f"GRANT {ASK_GRANT} s locally", "REVOKE the grant locally at the end"])
    case, out = make_campaign(bench_client, cfg, "T7", __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, "T7", out, preconditions=f"{cfg['chip']} declared {cfg['transport']}",
                  limits="Trigger on the ROM banner after an explicit reset; the card restarts several times on purpose; no flash access; "
                         "locking/concurrency of the deferred action is not observable here (engine tests only); one run"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        if "reset" not in bench_client.device(cfg["board_id"]).get("capabilities", []) or "trigger" not in bench_client.device(cfg["board_id"]).get("capabilities", []):
            raise Blocked("the card offers no reset / trigger capability on this interface")
        board = case.reserve(ttl=600)
        operator_gesture(case, f"GRANT {ASK_GRANT} s locally in the page", timeout=cfg["operator_timeout_s"])
        try:
            case.client.wait_device(cfg["board_id"], timeout=cfg["operator_timeout_s"], grant_seconds=MIN_GRANT, recovered=True)
        except TimeoutError as error:
            raise Blocked("Local grant not observed") from error
        cases = []
        # --- fire_once -------------------------------------------------------------------------------------------------
        arm(board, marker, DELAY_A)
        cursor = board.cursor()
        board.command("reset", {}, timeout=60)
        banners, triggers = observe(board, cursor, marker, OBSERVE_A)
        cases.append({"name": "fire_once", "delay_ms": DELAY_A, "observe_s": OBSERVE_A, "banners": banners, "triggers": triggers})
        # --- rearm_cancels ---------------------------------------------------------------------------------------------
        arm(board, marker, DELAY_B)
        cursor = board.cursor()
        board.command("reset", {}, timeout=60)
        banners, triggers, deadline = [], [], time.monotonic() + 15
        first, tail = None, b""
        while first is None and time.monotonic() < deadline:             # wait for banner #1 and its trigger event
            for event in board.events(cursor):
                if event["kind"] == "trigger":
                    triggers.append({"seq": event["seq"], "at": event.get("at")})
                elif event["kind"] == "uart":
                    data = tail + base64.b64decode(event["data"])
                    tail = data[-(len(marker) - 1):]
                    if marker in data:
                        banners.append({"seq": event["seq"], "at": event.get("at")})
                        first = event["seq"]
            time.sleep(0.05)
        assert first is not None, "banner #1 not seen after the reset"
        board.command("trigger", {"data": base64.b64encode(b"IOBEWI-T7-NEVER-STIMULATED").decode(), "action": "mark", "delay_ms": 0, "expires_ms": 30000})
        more_banners, more_triggers = observe(board, cursor, marker, OBSERVE_B)
        cases.append({"name": "rearm_cancels", "delay_ms": DELAY_B, "observe_s": OBSERVE_B,
                      "banners": banners + more_banners, "triggers": triggers + more_triggers})
        board.command("trigger", {"action": "disarm"})
        case.write("trigger-observations.json", {"marker_len": len(marker), "cases": cases})
        for item in cases:
            out.evidence.append(f"{item['name']}: banners={len(item['banners'])} trigger_events={len(item['triggers'])} (delay {item['delay_ms']} ms)")
        fire, cancel = cases
        assert len(fire["triggers"]) == 1, f"fire_once: {len(fire['triggers'])} trigger event(s)"
        assert len(fire["banners"]) == 2, f"fire_once: {len(fire['banners'])} banner(s), expected the stimulus and the deferred reset"
        gap_ms = (fire["banners"][1]["at"] - fire["banners"][0]["at"]) * (1000 if fire["banners"][0]["at"] < 1e11 else 1)
        assert gap_ms >= 0.8 * DELAY_A, f"fire_once: deferred reset after {gap_ms:.0f} ms, before 0.8*{DELAY_A} ms"
        assert len(cancel["banners"]) == 1 and len(cancel["triggers"]) == 1, (
            f"rearm_cancels: {len(cancel['banners'])} banner(s), {len(cancel['triggers'])} trigger event(s): the pending reset was not cancelled")
        out.evidence.append("fire_once: exactly one trigger event and one deferred reset, no third banner")
        out.evidence.append("rearm_cancels: re-arming before the delay cancelled the pending deferred reset")
        operator_gesture(case, "REVOKE the grant locally in the page", timeout=cfg["operator_timeout_s"])
        wait_until(lambda: bench_client.device(cfg["board_id"])["privileged_seconds"] == 0, timeout=cfg["operator_timeout_s"], what="local grant revocation")

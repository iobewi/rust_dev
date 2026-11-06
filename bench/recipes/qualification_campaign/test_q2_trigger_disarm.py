"""Q2 - trigger disarm after loss of the local grant. No flash access, no mutation.

Uses a `mark` trigger with an inert pattern that is never stimulated. This recipe verifies
*disarm and refusal behaviour only*; it does NOT claim that a trigger can no longer fire after the
grant is lost - that needs a controlled stimulus and is a separate scenario (see qualification-scenarios.md).
Operator gestures (local grant / revocation) are announced and then observed with bounded waits;
this client never sends a grant. Hardware status: NOT executed (software-reviewed only).

Cases (each needs its own local grant):
  Q2a natural expiry   - shortest grant, operator does nothing
  Q2b local revocation - grant >= 300 s, operator revokes on request
  Q2c ownership        - same owner, NEW reservation: disarm refused trigger_not_owned while grant is valid
  Q2d identity         - a different identity (BENCH_CLIENT_TOKEN_B), grant valid: trigger_not_owned
Q2c/Q2d conclude ONLY the refusal of the new reservation/identity: releasing the arming reservation revokes
the lease and the agent cancels its trigger, so they do not show the behaviour of a still-armed trigger.
"""
import os
import time

import pytest

from iobewi_bench.qualification import expect_http_refusal

from .harness import Blocked, Outcome, make_campaign, operator_gesture, recorded, wait_until
from .session import build_manifest

pytestmark = pytest.mark.bench
# Minimum REMAINING grant accepted at the first observation. It is deliberately below the duration
# asked of the operator (a grant of N s is already < N s when first seen) yet long enough for the case.
ASK_SHORT, MIN_SHORT = 60, 10          # Q2a: shortest local grant; only needs to outlast one poll
ASK_REVOKE, MIN_REVOKE = 300, 150      # Q2b: arm + <=60 s revocation window + checks, with margin
ASK_OWNER, MIN_OWNER = 90, 45          # Q2c/Q2d: must still be valid at the refused disarm


def _case(bench_client, cfg, test_id, file, pytestconfig):
    manifest = build_manifest(cfg, "Q2", regions=[], steps=["Local grant as requested", "Revocation as requested"])
    return make_campaign(bench_client, cfg, test_id, file, pytestconfig, manifest=manifest)


def _arm(board, cfg, *, expires_ms=None):
    return board.command("trigger", {"data": cfg["q2"]["trigger_pattern_b64"], "action": "mark", "delay_ms": 0,
                                     "expires_ms": expires_ms or cfg["q2"]["expires_ms"]})


def _grant(bench_client, cfg):
    return bench_client.device(cfg["board_id"])["privileged_seconds"]


def _wait_grant(case, cfg, minimum, text):
    operator_gesture(case, text, timeout=cfg["operator_timeout_s"])
    try:
        return case.client.wait_device(cfg["board_id"], timeout=cfg["operator_timeout_s"],
                                       grant_seconds=minimum, recovered=True)["privileged_seconds"]
    except TimeoutError as error:
        raise Blocked("Local grant not observed") from error


def _after_grant_loss(board, case, cfg, out, bench_client):
    """Shared assertions once the grant is zero: rearm refused for the grant, then owner disarm."""
    assert _grant(bench_client, cfg) == 0
    expect_http_refusal(lambda: _arm(board, cfg), status=403, code="grant_required")
    assert _grant(bench_client, cfg) == 0, "Grant reappeared without an operator"
    board.command("trigger", {"action": "disarm"})        # exactly this payload, same active reservation
    assert _grant(bench_client, cfg) == 0, "Disarm must not renew or re-grant"
    out.evidence.append("rearm refused 403 grant_required; owner disarm settled; grant still 0")


def _natural_or_revoked(pytestconfig, bench_client, qual_session, *, revoke):
    cfg, test_id = qual_session, "Q2b" if revoke else "Q2a"
    case = _case(bench_client, cfg, test_id, __file__, pytestconfig)
    out = Outcome()
    with recorded(case, test_id, out, preconditions=f"{cfg['chip']} / {cfg['transport']}, mark trigger, no flash access",
                  limits="Disarm/refusal only; no stimulus, so nothing is concluded about firing; one run"):
        case.start()
        board = case.reserve(ttl=cfg["q2"]["reservation_ttl_s"])
        if revoke:
            granted = _wait_grant(case, cfg, MIN_REVOKE, f"GRANT {ASK_REVOKE} s locally; then wait for the revoke request")
        else:
            granted = _wait_grant(case, cfg, MIN_SHORT, f"GRANT the SHORTEST local duration ({ASK_SHORT} s) and then do nothing")
            if granted + 20 > cfg["q2"]["expires_ms"] / 1000:
                raise Blocked(f"Grant {granted:.0f} s outlasts the trigger lifetime; pick a shorter one")
        started = time.monotonic()
        _arm(board, cfg)
        out.evidence.append(f"armed with {granted:.0f} s grant remaining")
        if revoke:
            operator_gesture(case, "REVOKE the grant locally NOW", timeout=60)
            last = granted
            def revoked():
                nonlocal last
                value = _grant(bench_client, cfg)
                if value > 0:
                    last = value
                return value == 0
            wait_until(revoked, timeout=60, interval=0.5, what="local revocation before the trigger lifetime ends")
            if last <= 10:
                raise Blocked("Grant ended by expiry, not by an operator revocation")
        else:
            wait_until(lambda: _grant(bench_client, cfg) == 0, timeout=granted + 30, what="natural grant expiry")
        if time.monotonic() - started > cfg["q2"]["expires_ms"] / 1000 - 5:
            raise Blocked("Trigger lifetime elapsed before the disarm check; rerun with faster gestures")
        _after_grant_loss(board, case, cfg, out, bench_client)


def test_q2a_natural_expiry(pytestconfig, bench_client, qual_session):
    _natural_or_revoked(pytestconfig, bench_client, qual_session, revoke=False)


def test_q2b_local_revocation(pytestconfig, bench_client, qual_session):
    _natural_or_revoked(pytestconfig, bench_client, qual_session, revoke=True)


def _ownership(pytestconfig, bench_client, cfg, test_id, other_token):
    case = _case(bench_client, cfg, test_id, __file__, pytestconfig)
    out = Outcome()
    with recorded(case, test_id, out, preconditions=f"{cfg['chip']} / {cfg['transport']}, mark trigger, valid grant",
                  limits="Refusal of a NEW reservation/identity while the grant is VALID (not a grant refusal); says nothing about a still-armed trigger (released lease cancels it); no stimulus"):
        case.start()
        first = case.reserve(ttl=cfg["q2"]["reservation_ttl_s"])
        _wait_grant(case, cfg, MIN_OWNER, f"GRANT {ASK_OWNER} s locally")
        _arm(first, cfg, expires_ms=30000)
        first.close()                                       # releases the reservation that armed the trigger
        if other_token:
            other = case.independent_client(other_token)
            assert other.identity()["id"] != case.client.identity()["id"], "Q2d needs two distinct identities"
            second = case.reserve(client=other, ttl=60)
        else:
            second = case.reserve(ttl=60)                     # same owner, NEW reservation
        assert _grant(bench_client, cfg) > 5, "Grant too short: refusal could not be attributed to ownership"
        expect_http_refusal(lambda: second.command("trigger", {"action": "disarm"}), status=403, code="trigger_not_owned")
        out.evidence.append("disarm by " + ("another identity" if other_token else "same owner, new reservation") +
                            " refused 403 trigger_not_owned with a valid grant")
        operator_gesture(case, "REVOKE the grant locally (end of ownership test)", timeout=cfg["operator_timeout_s"])
        wait_until(lambda: _grant(bench_client, cfg) == 0, timeout=cfg["operator_timeout_s"], what="local revocation")


def test_q2c_new_reservation_cannot_disarm(pytestconfig, bench_client, qual_session):
    _ownership(pytestconfig, bench_client, qual_session, "Q2c", None)


def test_q2d_other_identity_cannot_disarm(pytestconfig, bench_client, qual_session):
    token = os.environ.get("BENCH_CLIENT_TOKEN_B")
    if not token:
        pytest.skip("BLOCKED: Q2d needs an independently configured second identity in BENCH_CLIENT_TOKEN_B")
    _ownership(pytestconfig, bench_client, qual_session, "Q2d", token)

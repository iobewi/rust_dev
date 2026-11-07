"""G9 - reservation expiry (issue #2, requirement 9). No flash access, no grant, no restart: the only card command is the harmless `mark`.

Criteria, written before any run:
  PASS     while a short reservation is active a second reservation is refused 409 device_reserved; the reservation then EXPIRES by itself
           (the device shows no reservation within [TTL-1, TTL+10] s of its creation); with the old handle a command and a renewal are both
           refused 409 lease_expired; a NEW reservation is then granted and works; the card is never quarantined by any of this;
  FAIL     the reservation never expires inside the bound, an expired handle is still accepted, or the card is quarantined;
  BLOCKED  card absent / declaration mismatch.
Requirement 9 also names two boards of the same family: see test_two_boards.py (hardware dependency). Hardware status: NOT executed.
"""
import time

import pytest

from iobewi_bench.qualification import expect_http_refusal

from .harness import Outcome, make_campaign, recorded, wait_until
from .readonly import record_transport, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench
TTL = 8


def test_g9_reservation_expires_by_itself(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    manifest = build_manifest(cfg, "G9", regions=[], steps=["No operator gesture"])
    case, out = make_campaign(bench_client, cfg, "G9", __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, "G9", out, preconditions=f"{cfg['chip']} declared {cfg['transport']}, reservation TTL {TTL} s",
                  limits="One expiry of a short reservation, no operation in flight (a running operation at expiry is a different, quarantining case); one run"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        created = time.monotonic()
        board = case.reserve(ttl=TTL)
        expect_http_refusal(lambda: case.reserve(ttl=TTL), status=409, code="device_reserved")
        out.evidence.append("a second reservation was refused 409 device_reserved while the first one was active")
        board.command("mark", {"label": "g9-before-expiry"})
        wait_until(lambda: bench_client.device(cfg["board_id"])["reservation"] is None, timeout=TTL + 10, interval=0.25, what="the reservation to expire")
        elapsed = time.monotonic() - created
        case.write("expiry.json", {"ttl_s": TTL, "device_free_after_s": round(elapsed, 2)})
        assert TTL - 1 <= elapsed <= TTL + 10, f"the reservation expired after {elapsed:.1f} s, not near its TTL of {TTL} s"
        out.evidence.append(f"the reservation expired by itself after {elapsed:.1f} s (TTL {TTL} s)")
        expect_http_refusal(lambda: board.command("mark", {"label": "g9-after-expiry"}), status=409, code="lease_expired")
        expect_http_refusal(lambda: board.renew(ttl=60), status=409, code="lease_expired")
        out.evidence.append("the expired handle was refused 409 lease_expired for a command and for a renewal")
        assert not bench_client.device(cfg["board_id"])["quarantined"], "the expiry quarantined the card"
        fresh = case.reserve(ttl=60)
        fresh.command("mark", {"label": "g9-new-reservation"})
        out.evidence.append("a new reservation was granted after the expiry and works; the card was never quarantined")

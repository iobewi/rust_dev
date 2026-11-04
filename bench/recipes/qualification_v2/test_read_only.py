"""Qualification v2. Read only; use the documented per-lot invocations."""
import time

import pytest

from iobewi_bench.qualification import (
    compare_reference, expect_disconnected_read, expect_http_refusal, expect_stale_board,
)

pytestmark = pytest.mark.bench
SIZE = 10 * 1024 * 1024


def test_m1_repeatable_read(qualification):
    board = qualification.reserve()
    identity = board.identify()
    assert identity["chip"] == "esp32s3"
    assert identity["flash_size"] >= 0x11000
    assert board.read_flash(address=0x10000, size=4096) == board.read_flash(address=0x10000, size=4096)


def test_m4_deliberate_failure(intentional_failure, qualification):
    board = qualification.reserve()
    assert len(board.read_flash(address=0x10000, size=256)) == 256
    pytest.fail("Deliberate qualification failure after a settled read")


def test_m5_concurrency(second_token, qualification):
    a = qualification.reserve(ttl=120)
    owner_a = qualification.client.device(qualification.device)["reservation"]["owner"]
    other = qualification.independent_client(second_token)
    expect_http_refusal(lambda: qualification.reserve(client=other, ttl=60),
                        status=409, code="device_reserved")
    assert len(a.read_flash(address=0x10000, size=256)) == 256
    a.close()
    b = qualification.reserve(client=other, ttl=60)
    owner_b = other.device(qualification.device)["reservation"]["owner"]
    assert owner_a != owner_b, "M5 requires two distinct client identities"
    assert len(b.read_flash(address=0x10000, size=256)) == 256


def test_m5_renewal(qualification):
    board = qualification.reserve(ttl=6)
    # Start the clock after the reservation response: exceeding this deadline
    # also exceeds the server's original expiry, irrespective of request latency.
    started = time.monotonic()
    time.sleep(2)
    board.renew(ttl=60)
    time.sleep(max(0, 7 - (time.monotonic() - started)))
    assert time.monotonic() - started >= 7
    assert len(board.read_flash(address=0x10000, size=256)) == 256


def test_m5_expiration(qualification):
    old = qualification.reserve(ttl=1)
    qualification.events()
    time.sleep(2.5)
    expect_http_refusal(old.identify, status=409, code="lease_expired")
    assert not any(e["kind"] == "operation" for e in qualification.events()), "Expired lease started work"
    old.close()
    board = qualification.reserve(ttl=60)
    assert len(board.read_flash(address=0x10000, size=256)) == 256


def test_m6_verified_long_read(reference, qualification):
    path, digest = reference
    # Validate the full reference before any read, then assert equality again
    # after the transfer. A digest proves transfer integrity, not board contents.
    import hashlib
    ref = path.read_bytes()
    assert len(ref) >= SIZE and hashlib.sha256(ref).hexdigest() == digest, "Invalid reference"
    board = qualification.reserve()
    assert board.identify()["flash_size"] >= SIZE
    started = time.monotonic()
    data = board.read_flash(address=0, size=SIZE, timeout=600)
    comparison = compare_reference(data, path, sha256=digest, size=SIZE)
    comparison["duration_seconds"] = time.monotonic() - started
    qualification.write("comparison.json", comparison)


def _wait_new_generation(qualification, generation, timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qualification.events()
        device = qualification.client.device(qualification.device)
        if device["connected"] and device["generation"] != generation:
            return device
        time.sleep(0.5)
    pytest.fail("No connected new generation within the coordinated gesture window")


def test_m8_interrupted_read(physical_gesture, qualification):
    board = qualification.reserve()
    assert board.identify()["flash_size"] >= SIZE
    qualification.events()
    print("M8 ready: unplug USB during the upcoming read, then reconnect the same card", flush=True)
    operation = expect_disconnected_read(board, size=SIZE)
    # No second command on the old Board: the first campaign did that too early.
    transition = _wait_new_generation(qualification, board.generation)
    expect_stale_board(board, qualification.client)
    device_events = []
    # Inspect already recorded safe events, including those drained while waiting.
    import json
    for line in (qualification.directory / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if "kind" in event:
            device_events.append(event)
    assert any(e["kind"] == "disconnected" for e in device_events), "Missing disconnection event"
    connected = next(e for e in device_events if e["kind"] == "connected"
                     and e.get("generation") == transition["generation"])
    assert not any(e["kind"] == "operation" and e["seq"] > connected["seq"] for e in device_events), "Operation after reconnect"
    assert board.output_dir is not None and not list(board.output_dir.glob("flash-*.bin"))
    qualification.write("interruption.json", {"operation": operation, "new_generation": transition["generation"],
                                             "old_board_submitted": False})


def test_m8b_stale_board(physical_gesture, qualification):
    board = qualification.reserve()
    board.identify()
    print("M8b ready: use Disconnect then Connect on the same card; page reload is insufficient", flush=True)
    _wait_new_generation(qualification, board.generation)
    expect_stale_board(board, qualification.client)

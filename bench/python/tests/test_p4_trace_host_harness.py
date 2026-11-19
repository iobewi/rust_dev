"""Native host harness of the P4 trace: runs the REAL patched C against a scripted transport.

Needs a clean esp-flasher-stub v1.2.2 checkout with its pinned submodule and a host C compiler;
set IDLAB_ESP_FLASHER_STUB to its path. Skipped otherwise (no network in the default CI).
Not a hardware test: the clock is simulated, only the logic of the instrumentation is checked.

Simulated costs (harness.c): flash 1000, md5 200, fill 50, ACK wait 700 cycles, and every read of
the counter advances it by 1. A fill measures cost+1, a flash/md5 interval cost+1, an ACK wait
cost+1, and a send is 1 + 52 per fill (each fill advances the clock by 50 + 2 counter reads).
"""
import importlib.util
import os
import shutil
from pathlib import Path

import pytest

CHECKOUT = os.environ.get("IDLAB_ESP_FLASHER_STUB")
pytestmark = pytest.mark.skipif(
    not CHECKOUT or not shutil.which("gcc"),
    reason="set IDLAB_ESP_FLASHER_STUB to an esp-flasher-stub v1.2.2 checkout (and have gcc)")

DIR = Path(__file__).resolve().parents[2] / "diagnostics" / "p4_usb_trace" / "host_harness"


@pytest.fixture(scope="module")
def results():
    spec = importlib.util.spec_from_file_location("run_host_harness", DIR / "run_host_harness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run(CHECKOUT)


def expected(packets, fills):
    return {"flash": packets * 1001, "md5": packets * 201, "fill": fills * 51,
            "send": packets + 52 * fills, "ack": packets * 701, "fill_calls": fills, "packets": packets,
            "ack_count": packets}


def test_nominal_traced_read_gives_exact_counters(results):
    r = results["s1_nominal"]
    # three 4096-byte packets (64 flushes + the end-of-frame flush each) and a 100-byte tail (2 flushes)
    want = expected(packets=4, fills=3 * 65 + 2)
    assert {k: r[k] for k in want} == want
    assert r["bytes"] == 3 * 4096 + 100
    assert (r["magic"], r["version"], r["status"]) == (0x31544449, 1, 0)


def test_fill_is_a_subset_of_send_and_must_not_be_added_to_it(results):
    r = results["s1_nominal"]
    assert r["fill"] < r["send"]
    # what send adds to fill is only the per-packet bookkeeping and the counter reads around each fill
    assert r["send"] - r["fill"] == r["packets"] + r["fill_calls"]


def test_the_md5_frame_and_the_command_response_are_not_traced(results):
    # world: 1 response + 4 data frames + the MD5 frame + the GET response = 7 frames
    assert results["s1_world"]["frames"] == 7
    assert results["s1_nominal"]["fill_calls"] == 197
    assert results["s1_world"]["rom_fill_calls"] > results["s1_nominal"]["fill_calls"]


def test_the_standard_read_flash_opcode_leaves_the_counters_untouched(results):
    assert results["s2_unchanged"]["value"] == 1


def test_a_flash_error_stops_the_trace_and_carries_the_error_to_the_next_command(results):
    assert results["s3_first_get"] == {"rc": 1, "status": 0xC4}  # RESPONSE_FAILED_SPI_OP, upstream carry-over
    partial = results["s3_partial_counters"]
    assert (partial["packets"], partial["ack_count"], partial["bytes"], partial["fill_calls"]) == (1, 1, 4096, 65)
    assert partial["flash"] == 1001 + 1  # the failed read is still timed
    assert results["s3_fill_counted_after_error"]["value"] == 0  # tracing really stopped


def test_a_malformed_ack_leaves_a_counter_mismatch_the_host_can_detect(results):
    r = results["s4_bad_ack"]
    assert r["packets"] == 1 and r["ack_count"] == 0 and r["bytes"] == 4096
    assert r["packets"] != r["ack_count"]  # p4-stub-trace.js rejects exactly this


def test_a_get_with_a_payload_is_refused(results):
    assert results["s5_get_with_payload"] == {"status_hi": 0xC0, "status_lo": 0x00}  # RESPONSE_BAD_DATA_LEN


def test_a_frozen_cycle_counter_makes_the_probe_fail_closed(results):
    assert results["s6_frozen_counter"] == {"rc": 1, "status": 0xFF}  # RESPONSE_CMD_NOT_IMPLEMENTED


def test_the_32_bit_counter_wrapping_during_the_run_does_not_change_any_delta(results):
    r = results["s7_wrap"]
    want = expected(packets=2, fills=2 * 65)
    assert {k: r[k] for k in want} == want


def test_the_accumulators_are_64_bit(results):
    r = results["s8_u64"]
    assert r["flash"] == 3 * 3_000_000_001 and r["flash"] > 2 ** 32

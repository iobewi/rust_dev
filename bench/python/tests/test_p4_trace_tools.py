"""Offline tests of the static qualifier and of the campaign analyzer (synthetic data only)."""
import base64
import copy
import importlib.util
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[2] / "diagnostics" / "p4_usb_trace"


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qualify, analyze = load("qualify_stub"), load("analyze_trace")


# ------------------------------------------------------------------ static qualifier
def image(traced=True):
    text, data = bytes(range(100)), bytes([0xDF, 0xE0]) + bytes(8)
    info = {"sha256": "0" * 64, "entry": 0x4FF50010, "rdcycle": 12 if traced else 0,
            "sections": {".text": {"addr": 0x4FF50000, "size": len(text), "data": text},
                         ".bss": {"addr": 0x4FF60000, "size": 64, "data": b""},
                         ".data": {"addr": 0x4FF60040, "size": len(data), "data": data}},
            "symbols": {"esp_main": {"addr": 0x4FF50010, "size": 4}}}
    if traced:
        info["symbols"].update({f"idlab_counter_{i}": {"addr": 0x4FF60000 + 4 * i, "size": 4} for i in range(10)})
        info["symbols"]["idlab_trace_opcodes"] = {"addr": 0x4FF60040, "size": 2}
    pad = lambda b: b + bytes(-len(b) % 4)  # noqa: E731
    loader = {"entry": info["entry"], "text_start": 0x4FF50000, "data_start": 0x4FF60040, "bss_start": 0x4FF60000,
              "text": base64.b64encode(pad(text)).decode(), "data": base64.b64encode(pad(data)).decode()}
    return info, loader


def failing(info, loader, traced=True):
    return [c["check"] for c in qualify.check_stub(info, loader, traced=traced) if not c["ok"]]


def test_a_consistent_instrumented_image_passes():
    assert failing(*image()) == []


def test_a_consistent_standard_image_passes():
    assert failing(*image(traced=False), traced=False) == []


def test_a_section_outside_its_region_fails():
    info, loader = image()
    info["sections"][".text"]["size"] = 0x4501
    assert ".text is inside iram" in failing(info, loader)
    info, loader = image()
    info["sections"][".data"]["addr"] = 0x4FF60000 + 0x18000 - 4
    assert ".data is inside dram" in failing(info, loader)


def test_overlapping_sections_fail():
    info, loader = image()
    info["sections"][".data"]["addr"] = 0x4FF60010
    assert "no two sections overlap" in failing(info, loader)


@pytest.mark.parametrize("key", ["entry", "text_start", "data_start", "bss_start"])
def test_a_loader_json_address_that_disagrees_with_the_elf_fails(key):
    info, loader = image()
    loader[key] += 4
    assert failing(info, loader)


def test_loader_json_padding_must_be_zeros_to_a_four_byte_boundary_exactly():
    info, loader = image()
    raw = base64.b64decode(loader["data"])
    loader["data"] = base64.b64encode(raw[:-1] + b"\x01").decode()   # non-zero padding byte
    assert any("data bytes" in name for name in failing(info, loader))
    info, loader = image()
    loader["text"] = base64.b64encode(base64.b64decode(loader["text"]) + bytes(4)).decode()  # too long
    assert any("text bytes" in name for name in failing(info, loader))


def test_the_loader_json_keys_must_be_exactly_the_expected_ones():
    info, loader = image()
    loader["extra"] = 1
    assert "the loader JSON has exactly the expected keys" in failing(info, loader)


def test_the_standard_image_must_carry_no_diagnostic():
    info, loader = image(traced=False)
    info["symbols"]["idlab_leak"] = {"addr": 0, "size": 1}
    info["rdcycle"] = 1
    assert {"the standard image has no diagnostic symbol",
            "the standard image never reads the cycle counter"} <= set(failing(info, loader, traced=False))


def test_the_instrumented_image_must_link_the_exact_opcode_table():
    info, loader = image()
    info["sections"][".data"]["data"] = bytes([0xDF, 0xE1]) + bytes(8)
    assert "the linked opcode table is exactly 0xDF (trace read) and 0xE0 (counters)" in failing(info, loader)
    info, loader = image()
    del info["symbols"]["idlab_trace_opcodes"]
    assert failing(info, loader)


def test_the_instrumented_image_must_read_the_counter_and_link_its_counters():
    info, loader = image()
    info["rdcycle"] = 3
    assert "the cycle counter is read by the image" in failing(info, loader)


def test_the_size_delta_is_reported():
    stock, _ = image(traced=False)
    traced, _ = image()
    traced["sections"][".text"]["size"] += 700
    assert qualify.compare(stock, traced)["size_delta_bytes"][".text"] == 700


# ------------------------------------------------------------------ campaign analyzer
CPU = 200_000_000
MIB = 1024 * 1024


def evidence(flash=400e6, md5=35e6, send=3.6e9, fill=3.4e9, ack=0.1e9, wall_ms=None, a1=23100, a2=23300, **over):
    counted = flash + md5 + send + ack
    wall_ms = wall_ms if wall_ms is not None else counted / CPU * 1000
    trace = {"flash_cycles": str(int(flash)), "md5_cycles": str(int(md5)), "send_cycles": str(int(send)),
             "ack_cycles": str(int(ack)), "fill_cycles": str(int(fill)), "fill_calls": 16384, "packets": 256,
             "ack_count": 256, "bytes": MIB}
    trace.update(over)
    sha = "ab" * 32
    return {"schema": analyze.SCHEMA, "region": {"address": 0x8000, "size": MIB, "packet_size": 4096},
            "runs": [{"label": "A1", "duration_ms": a1, "md5": "cd" * 16, "sha256": sha},
                     {"label": "D", "duration_ms": 2175, "md5": "cd" * 16},
                     {"label": "T", "duration_ms": wall_ms, "md5": "cd" * 16, "sha256": sha, "trace": trace},
                     {"label": "A2", "duration_ms": a2, "md5": "cd" * 16, "sha256": sha}]}


def test_the_rom_fifo_fill_is_named_when_it_holds_most_of_the_time():
    report = analyze.analyze(evidence(), CPU)
    assert report["verdict"] == "ROM_CDC_FIFO_FILL"
    assert report["shares_of_counted_cycles"]["fill"] > 0.8


def test_slip_encoding_is_named_when_send_is_slow_but_fill_is_not():
    assert analyze.analyze(evidence(fill=0.1e9), CPU)["verdict"] == "SLIP_ENCODING_AND_SEND"


def test_the_ack_wait_is_named_when_the_host_return_path_dominates():
    report = analyze.analyze(evidence(send=0.2e9, fill=0.15e9, ack=3.5e9), CPU)
    assert report["verdict"] == "ACK_WAIT_AND_HOST_SYNC"


def test_reading_the_flash_is_named_only_if_it_dominates():
    assert analyze.analyze(evidence(flash=3.5e9, send=0.2e9, fill=0.1e9, ack=0.1e9), CPU)["verdict"] == "READING_FLASH"


def test_no_phase_above_half_gives_no_dominant_verdict():
    report = analyze.analyze(evidence(flash=1e9, md5=1e9, send=1e9, fill=0.4e9, ack=1e9), CPU)
    assert report["verdict"] == "NO_SINGLE_DOMINANT_PHASE"


def test_fill_is_never_added_to_send_in_the_microsecond_conversion():
    report = analyze.analyze(evidence(), CPU)
    us = report["microseconds"]
    assert us["send_without_fill"] == pytest.approx(us["send_total"] - us["fill_within_send"])
    assert us["counted_total"] == pytest.approx(us["flash"] + us["md5"] + us["send_total"] + us["ack_wait"])


def test_cycles_and_microseconds_are_separate_and_the_raw_cycles_are_untouched():
    report = analyze.analyze(evidence(), CPU)
    assert report["cycles"]["send_cycles"] == 3_600_000_000
    assert "microseconds" in report and report["cpu_hz_used"] == CPU


def test_the_unattributed_part_of_the_excess_is_reported():
    ex = analyze.analyze(evidence(), CPU)["excess_over_md5_only"]
    assert ex["excess_ms"] == pytest.approx(23200 - 2175)
    assert ex["unattributed_ms"] == pytest.approx(ex["excess_ms"] - ex["send_plus_ack_ms"])


def test_a_data_difference_makes_the_whole_campaign_invalid():
    e = evidence()
    e["runs"][3]["sha256"] = "00" * 32
    report = analyze.analyze(e, CPU)
    assert report["verdict"] == "INVALID" and "microseconds" not in report
    e = evidence()
    e["runs"][1]["md5"] = "11" * 16
    assert analyze.analyze(e, CPU)["verdict"] == "INVALID"


@pytest.mark.parametrize("override", [{"ack_count": 255}, {"packets": 255}, {"bytes": MIB - 4096}])
def test_a_trace_that_disagrees_with_the_transfer_is_invalid(override):
    assert analyze.analyze(evidence(**override), CPU)["verdict"] == "INVALID"


def test_fill_larger_than_send_is_invalid():
    assert analyze.analyze(evidence(send=1e9, fill=2e9), CPU)["verdict"] == "INVALID"


def test_a_zero_counter_is_invalid():
    assert analyze.analyze(evidence(md5=0), CPU)["verdict"] == "INVALID"


def test_two_standard_reads_that_disagree_give_no_conclusion():
    assert analyze.analyze(evidence(a1=23000, a2=30000), CPU)["verdict"] == "UNSTABLE"


def test_a_wrong_clock_assumption_is_detected_by_the_wall_time_cross_check():
    # the counters really ran at 100 MHz but 200 MHz is assumed: the counted cycles cover half the wall time
    e = evidence(wall_ms=2 * (400e6 + 35e6 + 3.6e9 + 0.1e9) / CPU * 1000)
    report = analyze.analyze(e, CPU)
    assert report["verdict"] == "CLOCK_ASSUMPTION_SUSPECT"
    assert report["clock_cross_check"]["ratio_to_assumed"] == pytest.approx(0.5, abs=0.01)


def test_a_missing_run_or_schema_is_refused():
    e = evidence()
    e["runs"] = e["runs"][:3]
    with pytest.raises(ValueError, match="missing run A2"):
        analyze.analyze(e, CPU)
    e = copy.deepcopy(evidence())
    e["schema"] = "other"
    with pytest.raises(ValueError, match="schema"):
        analyze.analyze(e, CPU)


def test_the_markdown_states_that_fill_is_part_of_send():
    text = analyze.render(analyze.analyze(evidence(), CPU))
    assert "ROM_CDC_FIFO_FILL" in text and "part of `send`" in text

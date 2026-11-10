"""Software checks of the M40 complement recipes (M40-12a, M40-11c/d, M40-4). SIMULATION ONLY: RAM simulated cards ('board', 'board2'),
no ESP32, no Web Serial. They prove the recipes' decisions (PASS / FAIL / BLOCKED / INDETERMINATE), the session validation and the evidence
layout, never anything about real cards."""
import json
import os
import selectors
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from iobewi_bench import BenchClient

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
from qualification_campaign import m40                                       # noqa: E402
from qualification_campaign.session import SessionError, validate_session   # noqa: E402
from test_m40_recipes import result, tamper                                 # noqa: E402,F401  (consistent synthetic observations)

RECIPE = "recipes/qualification_campaign/test_m40_complement.py"
TOKEN = "pytest-client-test-token"
EXAMPLE = BENCH / "recipes/qualification_campaign/session.m40.example.json"


# --- fixtures and builders ------------------------------------------------------------------------------------------------
def image_header(chip_id=9, segments=3, magic=0xE9):
    raw = bytearray(24)
    raw[0], raw[1], raw[2], raw[3] = magic, segments, 2, 0x40
    raw[12:14] = chip_id.to_bytes(2, "little")
    raw[23] = 1
    return bytes(raw)


def table(entries):
    raw = b"".join(b"\xaa\x50" + struct.pack("<BBII", kind, 0x10, start, size) + label.encode().ljust(16, b"\0") + struct.pack("<I", 0)
                   for label, kind, start, size in entries)
    return raw.ljust(4096, b"\xff")


TABLE = [("nvs", 1, 0x11000, 0x6000), ("ota_0", 0, 0x20000, 0x100000), ("ota_1", 0, 0x120000, 0x100000)]
PRELOAD = [{"address": 0, "hex": image_header().hex()}, {"address": 0x10000, "hex": table(TABLE).hex()},
           {"address": 0x20000, "hex": image_header().hex()}]                       # ota_1 stays erased
BOOT = ["ESP-ROM:esp32s3-sim", "boot: second stage", "APP-READY"]


def session(**change):
    document = {"version": 1, "board_id": "board", "chip": "esp32s3", "flash_size": 4194304, "transport": "external_uart",
                "partition_table_offset": None, "evidence_root": "/private/evidence", "accept_unknown_transport": True,
                "consent": {"scenarios": ["M40-12A", "M40-11C"]}, "second_board": {"board_id": "board2"},
                "m40": {"sample_interval_s": 0.1,
                        "known": {"table_offset": 0x10000, "rom_marker": "ESP-ROM:esp32s3", "bootloader_marker": "boot:",
                                  "app_marker": "APP-READY", "marker_timeout_s": 5},
                        "uart": {"window_s": 5, "min_bytes": 100, "min_overlap_s": 2, "min_interleavings": 5, "stimulus": "none",
                                 "markers": {"board": ["board|"], "board2": ["board2|"]}}}}
    return {**document, **change}


@pytest.fixture
def sim():
    started = []

    def start(env=None):
        process = subprocess.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--second", "--chip", "esp32s3"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, **(env or {})})
        started.append(process)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10)
        url = json.loads(process.stdout.readline())["url"]
        client = BenchClient(url, TOKEN)
        deadline = time.monotonic() + 10
        while len(client.devices()) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return url, client
    yield start
    for process in started:
        process.terminate()
        process.communicate(timeout=5)


def run_recipe(tmp_path, url, selector, document, *, extra=()):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({**document, "evidence_root": str(tmp_path / "evidence")}))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": TOKEN, "BENCH_URL": url, "BENCH_QUAL_SESSION": str(path)}
    junit = tmp_path / "junit.xml"
    run = subprocess.run([sys.executable, "-m", "pytest", f"{RECIPE}::{selector}", "--bench", "--qual-allow-dirty", "--bench-results",
                          str(tmp_path / "runs"), f"--junitxml={junit}", "-q", "-rs", "-p", "no:cacheprovider", "--tb=short", *extra],
                         cwd=BENCH, env=env, capture_output=True, text=True, timeout=240)
    reports = {i["id"]: i for f in (tmp_path / "evidence").glob("*/report.json") for i in json.loads(f.read_text())["tests"]}
    return run, reports, junit


def posts(directory):
    rows = [json.loads(line) for path in directory.glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows if r["method"] == "POST" and r["path"] in ("/api/v1/operations", "/api/v1/leases")]


def commands(directory, board="board-0"):
    return [json.loads(line)["command"] for line in next(directory.glob(f"*/{board}/commands.jsonl")).read_text().splitlines()]


# --- pure decisions: marks / contiguity / reservations ------------------------------------------------------------------------
def ev(seq, kind, **extra):
    return {"seq": seq, "kind": kind, **extra}


def test_marks_are_exactly_the_submitted_labels():
    assert m40.check_marks([ev(1, "mark", detail="a"), ev(2, "uart")], ["a"]) == []
    assert m40.check_marks([ev(1, "mark", detail="b")], ["a"])                 # foreign label
    assert m40.check_marks([], ["a"])                                          # lost
    assert m40.check_marks([ev(1, "mark", detail="a"), ev(2, "mark", detail="a")], ["a"])   # duplicated
    assert m40.check_marks([ev(1, "mark", detail="a")], [])                    # unexpected mark


def test_event_kinds_and_contiguity():
    assert m40.check_event_kinds([ev(1, "mark"), ev(2, "operation")], {"mark", "operation", "uart"}) == []
    assert m40.check_event_kinds([ev(1, "transport")], {"mark"}) and m40.check_event_kinds([ev(1, "grant")], {"mark"})
    assert m40.check_contiguous([ev(11, "mark"), ev(12, "uart")], 10) == []
    assert m40.check_contiguous([ev(11, "mark"), ev(13, "uart")], 10)          # gap
    assert m40.check_contiguous([ev(11, "mark"), ev(11, "uart")], 10)          # duplicate
    assert m40.check_contiguous([ev(12, "mark")], 10)                          # first event missing


def test_mark_operation_checks():
    good = {"device": "a", "generation": "g", "command": "mark", "state": "completed", "result": {"label": "x"}}
    assert m40.check_mark_operation(good, device="a", generation="g", label="x") == []
    for change in ({"device": "b"}, {"generation": "h"}, {"command": "identify"}, {"state": "failed"}, {"result": {"label": "y"}}, {"result": None}):
        assert m40.check_mark_operation({**good, **change}, device="a", generation="g", label="x")


def test_reservation_expectations():
    held, free = {"reservation": {"owner": "c"}}, {"reservation": None}
    assert m40.check_reservations(held, held, first_held=True, second_held=True) == []
    assert m40.check_reservations(free, held, first_held=False, second_held=True) == []
    assert m40.check_reservations(free, free, first_held=False, second_held=True)       # releasing the first released the second
    assert m40.check_reservations(held, held, first_held=False, second_held=True)


# --- known content ------------------------------------------------------------------------------------------------------------
def test_image_header_is_checked_independently():
    facts, problems = m40.check_image_header(image_header(), "esp32s3")
    assert problems == [] and facts == {"magic": 0xE9, "segments": 3, "chip_id": 9}
    assert m40.check_image_header(image_header(magic=0x00), "esp32s3")[1]
    assert m40.check_image_header(image_header(segments=0), "esp32s3")[1] and m40.check_image_header(image_header(segments=17), "esp32s3")[1]
    assert m40.check_image_header(image_header(chip_id=5), "esp32s3")[1]                 # another chip's image
    assert m40.check_image_header(b"\xff" * 4096, "esp32s3")[1], "an erased region is never a valid header"
    assert m40.check_image_header(b"\xe9" * 8, "esp32s3")[1] and m40.check_image_header(image_header(), "esp32p4")[1]
    assert m40.is_erased(b"\xff" * 16) and not m40.is_erased(b"\xff" * 15 + b"\x00") and not m40.is_erased(b"")


def test_table_agreement_and_application_partitions():
    rows = [{"label": "nvs", "type": 1, "address": 0x11000, "size": 0x6000}, {"label": "ota_1", "type": 0, "address": 0x120000, "size": 4},
            {"label": "ota_0", "type": 0, "address": 0x20000, "size": 4}]
    assert [r["label"] for r in m40.app_partitions(rows)] == ["ota_0", "ota_1"]
    api = [{"label": "a", "address": 1, "size": 2}]
    assert m40.check_table_agreement(api, [("a", 1, 2)]) == []
    assert m40.check_table_agreement(api, [("a", 1, 3)]) and m40.check_table_agreement(api, []) and m40.check_table_agreement(api, [("b", 1, 2)])


# --- markers -------------------------------------------------------------------------------------------------------------------
def uart_event(seq, data, **extra):
    import base64
    return {"seq": seq, "kind": "uart", "data": base64.b64encode(data).decode(), **extra}


WANTED = [("rom", b"ESP-ROM"), ("bootloader", b"boot:"), ("application", b"APP")]


def test_markers_are_located_in_order_with_their_sequence():
    chunks = m40.decode_chunks([uart_event(5, b"ESP-ROM:x\r\n"), uart_event(6, b"I (1) boot: a\r\n"), uart_event(7, b"APP up\r\n")])
    located = m40.locate_markers(chunks, WANTED)
    assert [i["found"] for i in located] == [True] * 3 and [i["seq"] for i in located] == [5, 6, 7] and m40.marker_problems(located) == []
    split = m40.locate_markers(m40.decode_chunks([uart_event(1, b"ESP-"), uart_event(2, b"ROM bo"), uart_event(3, b"ot: APP")]), WANTED)
    assert [i["found"] for i in split] == [True] * 3, "a marker split across two events is still found"


def test_out_of_order_missing_and_absent_markers_are_distinguished():
    reverse = m40.locate_markers(m40.decode_chunks([uart_event(1, b"APP"), uart_event(2, b"boot:"), uart_event(3, b"ESP-ROM")]), WANTED)
    problems = m40.marker_problems(reverse)
    assert any("out of order" in p for p in problems) and reverse[0]["found"] and not reverse[1]["found"]
    missing = m40.locate_markers(m40.decode_chunks([uart_event(1, b"ESP-ROM"), uart_event(2, b"boot:")]), WANTED)
    assert m40.marker_problems(missing) == ["application marker never seen"]
    assert all(not i["found"] for i in m40.locate_markers([], WANTED))
    skipped = m40.locate_markers(m40.decode_chunks([uart_event(1, b"ESP-ROM"), uart_event(2, b"APP")]), WANTED)
    assert not skipped[1]["found"] and not skipped[2]["found"], "nothing can be ordered after a missing marker"


# --- simultaneous UART -------------------------------------------------------------------------------------------------------
def stream(card_tag, start, count, step=0.1, first_seq=1):
    return [uart_event(first_seq + i, card_tag + b"xxxx", at=start + i * step, source_seq=i + 1, source_lost=False) for i in range(count)]


def test_activity_overlap_and_alternation():
    a, b = stream(b"A|", 100.0, 30), stream(b"B|", 100.05, 30)
    act_a, act_b = m40.uart_activity(a), m40.uart_activity(b)
    assert act_a["bytes"] == 30 * 6 and act_a["events"] == 30
    assert m40.window_overlap(act_a, act_b) == pytest.approx(2.85, abs=0.01) and m40.interleavings(a, b) == 59
    later = m40.uart_activity(stream(b"B|", 200.0, 5))
    assert m40.window_overlap(act_a, later) == 0.0 and m40.interleavings(a, stream(b"B|", 200.0, 5)) == 1
    assert m40.window_overlap(act_a, m40.uart_activity([])) == 0.0 and m40.uart_activity([])["bytes"] == 0


def test_uart_continuity_violations():
    good = stream(b"A|", 1.0, 5)
    assert m40.check_uart_continuity(good) == []
    assert m40.check_uart_continuity([{**good[0], "source_lost": True}] + good[1:])
    jump = [dict(e) for e in good]
    jump[3]["source_seq"] = 9
    assert m40.check_uart_continuity(jump)
    assert m40.check_uart_continuity([good[1], good[0]]), "reordered events"
    assert m40.check_uart_continuity(good + [good[-1]]), "a duplicated event"


def test_streams_must_hold_their_own_markers_and_none_of_the_other():
    own = {"A": [b"A|"], "B": [b"B|"]}
    clean = {"A": stream(b"A|", 1, 3), "B": stream(b"B|", 1, 3)}
    assert m40.check_uart_isolation(clean, own) == []
    mixed = {"A": stream(b"A|", 1, 3) + [uart_event(9, b"B|", at=2)], "B": stream(b"B|", 1, 3)}
    assert any("mixed streams" in p for p in m40.check_uart_isolation(mixed, own))
    assert any("none of its own" in p for p in m40.check_uart_isolation({"A": stream(b"Z|", 1, 3), "B": stream(b"B|", 1, 3)}, own))
    assert any("none of its own" in p for p in m40.check_uart_isolation({"A": [], "B": stream(b"B|", 1, 3)}, own))


# --- session --------------------------------------------------------------------------------------------------------------------
def test_new_scenarios_and_sections_validate():
    cfg = validate_session(session())
    assert cfg["m40"]["known"]["table_offset"] == 0x10000 and cfg["m40"]["uart"]["stimulus"] == "none" and "read" not in cfg["m40"]
    assert validate_session(session(consent={"scenarios": ["M40-4-RESET"]}))
    example = validate_session(json.loads(EXAMPLE.read_text()))
    assert example["consent"]["scenarios"] == [] and example["m40"]["known"]["app_marker"].startswith("REPLACE-")
    assert all(m.startswith("REPLACE-") for items in example["m40"]["uart"]["markers"].values() for m in items)


@pytest.mark.parametrize("known", [
    {}, {"table_offset": 0x10000}, {"table_offset": 0x10001, "rom_marker": "a", "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": 5},
    {"table_offset": 0x400000, "rom_marker": "a", "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": 5},
    {"table_offset": -4096, "rom_marker": "a", "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": 5},
    {"table_offset": 0x10000, "rom_marker": "a", "bootloader_marker": "a", "app_marker": "c", "marker_timeout_s": 5},
    {"table_offset": 0x10000, "rom_marker": "", "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": 5},
    {"table_offset": 0x10000, "rom_marker": " a", "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": 5},
    {"table_offset": 0x10000, "rom_marker": "a\n", "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": 5},
    {"table_offset": 0x10000, "rom_marker": "a" * 129, "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": 5},
    {"table_offset": 0x10000, "rom_marker": "a", "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": 4},
    {"table_offset": 0x10000, "rom_marker": "a", "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": 121},
    {"table_offset": 0x10000, "rom_marker": "a", "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": True},
    {"table_offset": 0x10000, "rom_marker": "a", "bootloader_marker": "b", "app_marker": "c", "marker_timeout_s": 5, "extra": 1}])
def test_invalid_known_sections_are_refused(known):
    with pytest.raises(SessionError):
        validate_session(session(m40={"known": known}))


def uart(**change):
    return {"window_s": 5, "min_bytes": 100, "min_overlap_s": 2, "min_interleavings": 5, "stimulus": "none",
            "markers": {"board": ["board|"], "board2": ["board2|"]}, **change}


@pytest.mark.parametrize("section", [
    {}, uart(window_s=4), uart(window_s=601), uart(window_s=True), uart(min_bytes=0), uart(min_bytes=1.5), uart(min_overlap_s=0.01),
    uart(min_interleavings=0), uart(stimulus="reset_a"), uart(stimulus="reset"), uart(markers={"board": ["x"]}),
    uart(markers={"board": ["x"], "board2": ["x"]}), uart(markers={"board": ["x", "x"], "board2": ["y"]}),
    uart(markers={"board": [], "board2": ["y"]}), uart(markers={"board": ["x"] * 1, "other": ["y"]}),
    uart(markers={"board": ["a", "b", "c", "d", "e"], "board2": ["y"]}), uart(markers={"board": [" x"], "board2": ["y"]}),
    {**uart(), "extra": 1}])
def test_invalid_uart_sections_are_refused(section):
    with pytest.raises(SessionError):
        validate_session(session(m40={"uart": section}))


def test_uart_needs_a_second_board():
    with pytest.raises(SessionError):
        validate_session(session(second_board={}, m40={"uart": uart()}))


# --- end to end against the simulated bench ---------------------------------------------------------------------------------
def test_m40_12a_passes_with_exactly_three_marks_and_isolated_events(sim, tmp_path):
    url, client = sim()
    run, reports, junit = run_recipe(tmp_path, url, "test_m40_12a_simultaneous_reservations_and_marks", session())
    assert run.returncode == 0 and "1 passed" in run.stdout, run.stdout[-2500:]
    assert reports["M40-12a"]["status"] == "PASS", reports["M40-12a"]
    folder = next((tmp_path / "evidence").glob("M40-12a-*"))
    assert commands(tmp_path / "evidence", "board-0") == ["mark"] and commands(tmp_path / "evidence", "board-second") == ["mark", "mark"]
    assert len([r for r in posts(tmp_path / "evidence") if r["path"] == "/api/v1/operations"]) == 3
    steps = [json.loads(line) for line in (folder / "m40-12a-steps.jsonl").read_text().splitlines()]
    assert [s["step"] for s in steps] == ["initial", "both reserved", "marks submitted", "first released", "both released"]
    assert steps[1]["board"]["reserved"] and steps[1]["board2"]["reserved"]
    assert not steps[3]["board"]["reserved"] and steps[3]["board2"]["reserved"], "the second card must stay reserved after the first is released"
    assert not steps[4]["board"]["reserved"] and not steps[4]["board2"]["reserved"] and all(s["wall"] > 0 for s in steps)
    assert (steps[2]["board"]["seq"] > steps[0]["board"]["seq"]) and (steps[2]["board2"]["seq"] > steps[0]["board2"]["seq"]), "marks add events"
    assert all(client.device(n)["reservation"] is None for n in ("board", "board2"))
    props = {p.get("name"): p.get("value") for p in ET.parse(junit).getroot().iter("property")}
    assert props["devices"] == "board,board2"


def test_m40_12a_is_blocked_before_any_command(sim, tmp_path):
    url, client = sim()
    for name, document in (("consent", session(consent={"scenarios": []})), ("second", session(second_board={}, m40={"sample_interval_s": 0.1}))):
        work = tmp_path / name
        work.mkdir()
        run, reports, _ = run_recipe(work, url, "test_m40_12a_simultaneous_reservations_and_marks", document)
        assert "BLOCKED" in run.stdout and not reports and not posts(work / "evidence")
    held = client.reserve("board2", ttl=120)                  # second card already reserved by someone: not ready
    try:
        work = tmp_path / "held"
        work.mkdir()
        run, reports, _ = run_recipe(work, url, "test_m40_12a_simultaneous_reservations_and_marks", session())
    finally:
        held.close()
    assert reports["M40-12a"]["status"] == "BLOCKED" and "reserved" in " ".join(reports["M40-12a"]["incidents"])


def test_m40_11cd_passes_with_known_content_and_ordered_markers(sim, tmp_path):
    url, client = sim({"SIM_FLASH_PRELOAD": json.dumps(PRELOAD), "SIM_CONSOLE_BOOT": json.dumps(BOOT), "SIM_UART_TAG": "1"})
    run, reports, junit = run_recipe(tmp_path, url, "test_m40_11c_11d_known_content_and_application_return", session())
    assert run.returncode == 0 and "1 passed" in run.stdout, run.stdout[-2500:]
    report = reports["M40-11cd"]
    assert report["status"] == "PASS", report
    text = " ".join(report["evidence"])
    assert "M40-11c: PASS" in text and "M40-11d: PASS" in text and "rom@seq" in text and "application@seq" in text
    folder = next((tmp_path / "evidence").glob("M40-11cd-*"))
    names = commands(tmp_path / "evidence")
    assert names[:2] == ["flash.read"] * 2 and names.count("console") == 1 and names[-1] == "console" and names.count("partitions") == 1
    assert len(names) == 2 + 1 + 2 + 2 * 2 + 1, names                  # header x2, table probe, table x2, two application slots x2, console
    reads = json.loads((folder / "m40-11c-reads.json").read_text())
    assert reads["header_facts"]["chip_id"] == 9 and len(reads["reads"]) == 8 and sum(r["erased"] for r in reads["reads"]) == 2
    markers = json.loads((folder / "m40-11d-markers.json").read_text())
    assert [m["name"] for m in markers["markers"]] == ["rom", "bootloader", "application"] and all(m["found"] for m in markers["markers"])
    assert any("console opened" in d for d in markers["transport"])
    for dump in (folder / "board-0").glob("flash-*"):
        assert (dump.stat().st_mode & 0o777) == 0o600, dump.name
    assert not client.artifacts()["artifacts"] and client.device("board")["reservation"] is None
    assert not [r for r in posts(tmp_path / "evidence") if r["path"] == "/api/v1/operations"][len(names):], "no extra operation"
    reads_file, markers_file = json.loads((folder / "m40-11c-reads.json").read_text()), json.loads((folder / "m40-11d-markers.json").read_text())
    assert reads_file["phase"] == "markers" and reads_file["error"] is None and reads_file["problems"] == [] and len(reads_file["reads"]) == 8
    assert markers_file["phase"] == "markers" and markers_file["error"] is None and markers_file["problems"] == []


@pytest.mark.parametrize("boot,fragment", [
    (["ESP-ROM:esp32s3-sim", "boot: x"], "application marker never seen"),
    (["APP-READY", "boot: x", "ESP-ROM:esp32s3-sim"], "out of order"),
    (["boot: x", "APP-READY"], "rom marker never seen")])
def test_m40_11d_fails_when_the_application_did_not_demonstrably_restart(sim, tmp_path, boot, fragment):
    url, client = sim({"SIM_FLASH_PRELOAD": json.dumps(PRELOAD), "SIM_CONSOLE_BOOT": json.dumps(boot), "SIM_UART_TAG": "1"})
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_11c_11d_known_content_and_application_return", session())
    assert run.returncode != 0 and reports["M40-11cd"]["status"] == "FAIL", reports
    assert fragment in run.stdout + " ".join(reports["M40-11cd"]["incidents"])
    assert "M40-11c: PASS" in " ".join(reports["M40-11cd"]["evidence"]), "the known-content criterion is judged separately"


@pytest.mark.parametrize("preload,fragment", [
    ([{"address": 0, "hex": image_header(chip_id=5).hex()}, {"address": 0x10000, "hex": table(TABLE).hex()}, {"address": 0x20000, "hex": image_header().hex()}],
     "chip id"),
    ([{"address": 0, "hex": image_header().hex()}, {"address": 0x10000, "hex": table(TABLE).hex()}], "no application slot holds a valid image header"),
    ([{"address": 0, "hex": image_header().hex()}, {"address": 0x10000, "hex": table(TABLE).hex()}, {"address": 0x20000, "hex": (b"\x01" * 64).hex()}],
     "neither a valid image header nor erased")])
def test_m40_11c_fails_on_wrong_known_content(sim, tmp_path, preload, fragment):
    url, client = sim({"SIM_FLASH_PRELOAD": json.dumps(preload), "SIM_CONSOLE_BOOT": json.dumps(BOOT), "SIM_UART_TAG": "1"})
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_11c_11d_known_content_and_application_return", session())
    assert reports["M40-11cd"]["status"] == "FAIL" and fragment in run.stdout + " ".join(reports["M40-11cd"]["incidents"])
    assert "M40-11d: PASS" in " ".join(reports["M40-11cd"]["evidence"]), "the console return is still performed and judged"
    assert commands(tmp_path / "evidence")[-1] == "console"


def test_m40_11cd_is_blocked_without_a_valid_table_and_still_returns_the_console(sim, tmp_path):
    url, client = sim({"SIM_CONSOLE_BOOT": json.dumps(BOOT)})                      # erased flash: no table anywhere
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_11c_11d_known_content_and_application_return", session())
    assert reports["M40-11cd"]["status"] == "BLOCKED" and "no valid partition table" in " ".join(reports["M40-11cd"]["incidents"])
    names = commands(tmp_path / "evidence")
    assert names[-1] == "console" and "flash.read" in names, names                  # the card is not left in the loader
    assert client.device("board")["reservation"] is None
    reads_file = json.loads(next((tmp_path / "evidence").glob("M40-11cd-*/m40-11c-reads.json")).read_text())
    assert reads_file["phase"] == "table" and reads_file["error"] == "Blocked" and len(reads_file["reads"]) == 2, "the reads done before the block are kept"


@pytest.mark.parametrize("change,fragment", [({"consent": {"scenarios": []}}, "M40-11C"),
                                              ({"m40": {"sample_interval_s": 0.1}}, "m40.known")])
def test_m40_11cd_is_blocked_before_any_command(sim, tmp_path, change, fragment):
    url, client = sim()
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_11c_11d_known_content_and_application_return", session(**change))
    assert "BLOCKED" in run.stdout and fragment in run.stdout and not reports and not posts(tmp_path / "evidence")


def test_m40_11cd_refuses_the_template_application_marker(sim, tmp_path):
    url, client = sim()
    document = session()
    document["m40"]["known"]["app_marker"] = "REPLACE-with-a-line-the-application-prints"
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_11c_11d_known_content_and_application_return", document)
    assert "BLOCKED" in run.stdout and "placeholder" in run.stdout and not posts(tmp_path / "evidence")


def test_m40_4_passes_on_simultaneous_tagged_streams_without_any_command(sim, tmp_path):
    url, client = sim({"SIM_UART_RATE": "20:16", "SIM_UART_TAG": "1"})
    run, reports, junit = run_recipe(tmp_path, url, "test_m40_4_simultaneous_uart", session())
    assert run.returncode == 0 and "1 passed" in run.stdout, run.stdout[-2500:]
    assert reports["M40-4"]["status"] == "PASS", reports["M40-4"]
    assert not posts(tmp_path / "evidence"), "passive capture sends nothing and reserves nothing"
    folder = next((tmp_path / "evidence").glob("M40-4-*"))
    activity = json.loads((folder / "m40-4-activity.json").read_text())
    assert activity["overlap_s"] >= 2 and activity["interleavings"] >= 5 and all(v["bytes"] >= 100 for v in activity["activity"].values())
    assert all(all(flags) for flags in activity["own_markers_received"].values())
    for name in ("m40-observed-a.jsonl", "m40-observed-b.jsonl"):
        assert (folder / name).stat().st_mode & 0o777 == 0o600 and "xxxx" not in (folder / name).read_text(), "no UART payload in the evidence"
    assert "xxxx" not in json.dumps(activity)


def test_m40_4_is_blocked_when_a_card_is_silent(sim, tmp_path):
    url, client = sim()                                                              # no SIM_UART_RATE: the bench provides no source
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_4_simultaneous_uart", session())
    assert reports["M40-4"]["status"] == "BLOCKED" and "no UART source" in " ".join(reports["M40-4"]["incidents"])
    assert not posts(tmp_path / "evidence")


def test_m40_4_fails_when_streams_carry_no_own_marker(sim, tmp_path):
    url, client = sim({"SIM_UART_RATE": "20:16"})                                    # both cards print, but untagged: sources cannot be told apart
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_4_simultaneous_uart", session())
    assert reports["M40-4"]["status"] == "FAIL" and "none of its own markers" in run.stdout + " ".join(reports["M40-4"]["incidents"])


@pytest.mark.parametrize("change,extra,fragment", [
    ({"consent": {"scenarios": []}}, ("--qual-allow-reset",), "M40-4-RESET"),
    ({"consent": {"scenarios": ["M40-4-RESET"]}}, (), "--qual-allow-reset")])
def test_a_reset_of_the_second_card_is_never_implied(sim, tmp_path, change, extra, fragment):
    url, client = sim({"SIM_UART_RATE": "20:16", "SIM_UART_TAG": "1"})
    document = session(**change)
    document["m40"]["uart"]["stimulus"] = "reset_both"
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_4_simultaneous_uart", document, extra=extra)
    assert "BLOCKED" in run.stdout and fragment in run.stdout and not reports and not posts(tmp_path / "evidence")


def test_m40_4_with_reset_both_sends_exactly_one_reset_per_card_when_consented(sim, tmp_path):
    url, client = sim({"SIM_UART_RATE": "20:16", "SIM_UART_TAG": "1"})
    document = session(consent={"scenarios": ["M40-4-RESET"]})
    document["m40"]["uart"]["stimulus"] = "reset_both"
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_4_simultaneous_uart", document, extra=("--qual-allow-reset",))
    assert run.returncode == 0 and reports["M40-4"]["status"] == "PASS", run.stdout[-2500:]
    assert commands(tmp_path / "evidence", "board-0") == ["reset"] and commands(tmp_path / "evidence", "board-second") == ["reset"]
    assert len([r for r in posts(tmp_path / "evidence") if r["path"] == "/api/v1/operations"]) == 2
    assert all(client.device(n)["reservation"] is None for n in ("board", "board2"))


# --- the recipes' wiring: an injected defect in a helper must turn into the right verdict --------------------------------------------
INJECT = "\n_injected = lambda *args, **kwargs: ['injected defect']\n"
DOUBLE_COUNT = ("\nimport iobewi_bench.qualification as _q\n_real_request = _q.ObservedClient._request\n\n\n"
                "def _counting_request(self, method, path, body=None, **kwargs):\n    if method == 'POST' and path == '/api/v1/operations':\n"
                "        self.submissions += 1\n    return _real_request(self, method, path, body, **kwargs)\n\n\n_q.ObservedClient._request = _counting_request\n")


def run_with_defect(tmp_path, url, selector, document, defect, *, extra=()):
    """Run the recipe from a COPY of the bench tree whose m40.py got `defect` appended (the real simulated cards stay healthy)."""
    import shutil
    copy = tmp_path / "bench"
    shutil.copytree(BENCH, copy, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".bench-runs", ".pytest_cache"))
    with (copy / "recipes/qualification_campaign/m40.py").open("a") as stream:
        stream.write(defect)
    path = tmp_path / "session.json"
    path.write_text(json.dumps({**document, "evidence_root": str(tmp_path / "evidence")}))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": TOKEN, "BENCH_URL": url, "BENCH_QUAL_SESSION": str(path)}
    run = subprocess.run([sys.executable, "-m", "pytest", f"{RECIPE}::{selector}", "--bench", "--qual-allow-dirty", "--bench-results",
                          str(tmp_path / "runs"), "-q", "-p", "no:cacheprovider", "--tb=short", *extra],
                         cwd=copy, env=env, capture_output=True, text=True, timeout=240)
    reports = {i["id"]: i for f in (tmp_path / "evidence").glob("*/report.json") for i in json.loads(f.read_text())["tests"]}
    return run, reports


@pytest.mark.parametrize("helper,verdict", [
    ("check_marks", "FAIL"), ("check_mark_operation", "FAIL"), ("check_reservations", "FAIL"), ("check_event_kinds", "FAIL"),
    ("check_contiguous", "FAIL"), ("check_commands", "FAIL")])
def test_m40_12a_fails_when_a_check_reports_a_problem(sim, tmp_path, helper, verdict):
    url, client = sim()
    run, reports = run_with_defect(tmp_path, url, "test_m40_12a_simultaneous_reservations_and_marks", session(), INJECT + f"{helper} = _injected\n")
    assert run.returncode != 0 and reports["M40-12a"]["status"] == verdict, (helper, run.stdout[-800:])
    assert "injected defect" in run.stdout + " ".join(reports["M40-12a"]["incidents"])


def test_m40_12a_counts_its_submissions(sim, tmp_path):
    url, client = sim()
    run, reports = run_with_defect(tmp_path, url, "test_m40_12a_simultaneous_reservations_and_marks", session(), DOUBLE_COUNT)
    assert reports["M40-12a"]["status"] == "FAIL" and "operations submitted, expected 3" in run.stdout + " ".join(reports["M40-12a"]["incidents"])


def test_m40_11cd_counts_its_submissions(sim, tmp_path):
    url, client = sim({"SIM_FLASH_PRELOAD": json.dumps(PRELOAD), "SIM_CONSOLE_BOOT": json.dumps(BOOT), "SIM_UART_TAG": "1"})
    run, reports = run_with_defect(tmp_path, url, "test_m40_11c_11d_known_content_and_application_return", session(), DOUBLE_COUNT)
    assert reports["M40-11cd"]["status"] == "FAIL" and "operations submitted, expected" in run.stdout + " ".join(reports["M40-11cd"]["incidents"])


@pytest.mark.parametrize("helper,verdict,fragment", [
    ("check_commands", "FAIL", "replay or unexpected command"), ("check_independent", "FAIL", "second card"),
    ("check_complete", "INDETERMINATE", "incomplete"), ("check_continuity", "INDETERMINATE", "continuity")])
def test_m40_11cd_wiring_of_replay_continuity_and_second_card_checks(sim, tmp_path, helper, verdict, fragment):
    url, client = sim({"SIM_FLASH_PRELOAD": json.dumps(PRELOAD), "SIM_CONSOLE_BOOT": json.dumps(BOOT), "SIM_UART_TAG": "1"})
    run, reports = run_with_defect(tmp_path, url, "test_m40_11c_11d_known_content_and_application_return", session(), INJECT + f"{helper} = _injected\n")
    assert run.returncode != 0 and reports["M40-11cd"]["status"] == verdict, (helper, run.stdout[-800:])
    assert fragment in run.stdout + " ".join(reports["M40-11cd"]["incidents"])


@pytest.mark.parametrize("helper,verdict,fragment", [
    ("check_uart_continuity", "FAIL", "injected defect"), ("check_uart_isolation", "FAIL", "injected defect"),
    ("check_independent", "FAIL", "injected defect"), ("check_complete", "INDETERMINATE", "incomplete")])
def test_m40_4_wiring_of_continuity_isolation_and_state_checks(sim, tmp_path, helper, verdict, fragment):
    url, client = sim({"SIM_UART_RATE": "20:16", "SIM_UART_TAG": "1"})
    run, reports = run_with_defect(tmp_path, url, "test_m40_4_simultaneous_uart", session(), INJECT + f"{helper} = _injected\n")
    assert run.returncode != 0 and reports["M40-4"]["status"] == verdict, (helper, run.stdout[-800:])
    assert fragment in run.stdout + " ".join(reports["M40-4"]["incidents"])


@pytest.mark.parametrize("key,value,fragment", [("min_overlap_s", 600, "overlap for"), ("min_interleavings", 100000, "alternates")])
def test_m40_4_thresholds_decide(sim, tmp_path, key, value, fragment):
    url, client = sim({"SIM_UART_RATE": "20:16", "SIM_UART_TAG": "1"})
    document = session()
    document["m40"]["uart"][key] = value
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_4_simultaneous_uart", document)
    assert reports["M40-4"]["status"] == "FAIL" and fragment in run.stdout + " ".join(reports["M40-4"]["incidents"])


AFTER_RELEASE = ("\n_real_reservations = check_reservations\n\n\ndef check_reservations(first, second, *, first_held, second_held):\n"
                 "    return ['injected defect'] if not first_held else _real_reservations(first, second, first_held=first_held, second_held=second_held)\n")
FINAL_MARKS = ("\n_real_marks = check_marks\n\n\ndef check_marks(events, expected):\n"
               "    return ['injected defect'] if len(expected) == 2 else _real_marks(events, expected)\n")


@pytest.mark.parametrize("defect,fragment", [(AFTER_RELEASE, "releasing the first card changed the second one"),
                                              (FINAL_MARKS, "injected defect")])
def test_m40_12a_each_late_check_is_live(sim, tmp_path, defect, fragment):
    """Defects that only appear AFTER the first card was released / in the final whole-run capture: the early checks cannot hide them."""
    url, client = sim()
    run, reports = run_with_defect(tmp_path, url, "test_m40_12a_simultaneous_reservations_and_marks", session(), defect)
    assert run.returncode != 0 and reports["M40-12a"]["status"] == "FAIL", run.stdout[-800:]
    assert fragment in run.stdout + " ".join(reports["M40-12a"]["incidents"])


def test_m40_4_usb_reenumeration_after_a_reset_is_indeterminate_never_a_pass(sim, tmp_path):
    url, client = sim({"SIM_UART_RATE": "20:16", "SIM_UART_TAG": "1", "SIM_REENUMERATE": "1"})
    document = session(consent={"scenarios": ["M40-4-RESET"]})
    document["m40"]["uart"]["stimulus"] = "reset_both"
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_4_simultaneous_uart", document, extra=("--qual-allow-reset",))
    assert run.returncode != 0 and reports["M40-4"]["status"] == "INDETERMINATE", run.stdout[-1200:]
    assert "PASS" != reports["M40-4"]["status"]


# --- M40-4 reset_both: thread termination, abandoned commands, active-mode observation ---------------------------------------------------
import threading                                                                  # noqa: E402


class FakeBoard:
    """A Board stand-in: records every command it is asked (a replay would show), and can fail or block."""

    def __init__(self, *, error=None, block=None):
        self.calls, self.error, self.block = [], error, block

    def command(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.block is not None:
            self.block.wait(30)
        if self.error is not None:
            raise self.error


def test_fire_concurrently_starts_every_command_once_and_reports_no_survivor():
    boards = {"a": FakeBoard(), "b": FakeBoard()}
    fired = m40.fire_concurrently(boards, "reset", join_timeout=5, timeout=7, start_timeout=9)
    assert fired.alive == [] and fired.errors == {}
    assert all(board.calls == [("reset", {"timeout": 7, "start_timeout": 9})] for board in boards.values()), "exactly one submission each"


def test_fire_concurrently_returns_errors_without_retrying():
    err = RuntimeError("boom")
    boards = {"a": FakeBoard(error=err), "b": FakeBoard()}
    fired = m40.fire_concurrently(boards, "reset", join_timeout=5)
    assert fired.errors == {"a": err} and fired.alive == [] and len(boards["a"].calls) == 1 and len(boards["b"].calls) == 1


def test_a_thread_still_running_after_the_deadline_is_reported_not_assumed_finished():
    gate = threading.Event()
    boards = {"a": FakeBoard(block=gate), "b": FakeBoard()}
    started = time.monotonic()
    fired = m40.fire_concurrently(boards, "reset", join_timeout=0.4)
    elapsed = time.monotonic() - started
    assert fired.alive == ["a"] and elapsed < 2, "the join is bounded by ONE deadline, not one per thread"
    assert len(boards["a"].calls) == 1, "the stuck command is never submitted again"
    assert [t for t in threading.enumerate() if t.name == "fire-a"][0].daemon, "a stuck thread must not keep the process alive"
    gate.set()                                                                       # release it


def test_every_card_gets_the_same_join_deadline():
    gate = threading.Event()
    boards = {"a": FakeBoard(block=gate), "b": FakeBoard(block=gate), "c": FakeBoard(block=gate)}
    started = time.monotonic()
    fired = m40.fire_concurrently(boards, "reset", join_timeout=0.5)
    assert sorted(fired.alive) == ["a", "b", "c"] and time.monotonic() - started < 1.5
    gate.set()


def stimulated(**change):
    outcome = result()
    for sample in outcome.samples:
        sample.update(change)
    return outcome


def test_a_stimulated_card_may_be_reserved_but_not_change_identity():
    assert m40.stimulated_continuity(stimulated(reservation={"owner": "c"})) == [] and m40.stimulated_violations(stimulated(reservation={"owner": "c"})) == []
    for change in ({"generation": "g9"}, {"agent": "other"}, {"connected": False}):
        assert m40.stimulated_continuity(stimulated(**change)), change
    for change in ({"quarantined": True}, {"privileged_seconds": 9}):
        assert m40.stimulated_violations(stimulated(**change)), change
        assert not m40.stimulated_continuity(stimulated(**change)), "a quarantine is a violation, not a continuity loss"


def test_a_stimulated_card_may_emit_operation_and_transport_events_but_nothing_else():
    ok = result(events=[ev(11, "operation"), ev(12, "transport"), ev(13, "uart")], seq=13)
    assert m40.stimulated_violations(ok) == [] and m40.check_complete(ok) == []
    for kind in ("mark", "trigger", "grant", "hold", "recover", "connected", "disconnected"):
        assert m40.stimulated_violations(result(events=[ev(11, kind)], seq=11)), kind


def test_active_mode_still_suffers_epoch_loss_and_gap_detection_through_the_same_integrity_checks():
    outcome = result(events=[ev(11, "operation"), ev(13, "uart")], seq=13)
    outcome.pages[0]["count"] = 2
    assert any("gap" in p for p in m40.check_complete(outcome))
    outcome = result(events=[ev(11, "operation")], seq=11)
    outcome.pages[0]["epoch"] = "epoch-2"
    assert any("epoch changed" in p for p in m40.check_complete(outcome))
    outcome = result(events=[ev(11, "operation")], seq=11)
    outcome.pages[0]["lost"] = 2
    assert any("lost" in p for p in m40.check_complete(outcome))
    assert any("did not terminate" in p for p in m40.check_complete(tamper(result(), terminated=False)))
    assert any("final synchronous" in p for p in m40.check_complete(tamper(result(), final_sampled=False)))


RESET_ENV = {"SIM_UART_RATE": "20:16", "SIM_UART_TAG": "1"}


def reset_session(**uart_change):
    document = session(consent={"scenarios": ["M40-4-RESET"]})
    document["m40"]["uart"] = {**document["m40"]["uart"], "stimulus": "reset_both", **uart_change}
    return document


def test_reset_both_observes_both_cards_from_before_the_stimulus_with_complete_integrity(sim, tmp_path):
    url, client = sim(RESET_ENV)
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_4_simultaneous_uart", reset_session(), extra=("--qual-allow-reset",))
    assert reports["M40-4"]["status"] == "PASS", run.stdout[-1500:]
    folder = next((tmp_path / "evidence").glob("M40-4-*"))
    for name in ("m40-observed-a.jsonl", "m40-observed-b.jsonl"):
        rows = [json.loads(line) for line in (folder / name).read_text().splitlines()]
        summary = rows[-1]
        assert summary["terminated"] and summary["final_sampled"] and summary["drained"] and not summary["epoch_changed"], name
        samples = [r for r in rows if r["type"] == "sample"]
        assert len(samples) >= 3 and rows[0]["type"] == "baseline" and rows[0]["reservation"] is None, "observation starts BEFORE any reservation"
        assert all(r["wall"] > 0 for r in samples) and (folder / name).stat().st_mode & 0o777 == 0o600
    steps = [json.loads(line)["step"] for line in (folder / "m40-4-steps.jsonl").read_text().splitlines()]
    assert steps == ["initial", "both reserved", "resets settled or abandoned", "both released"]


@pytest.mark.parametrize("defect,verdict,fragment", [
    ("check_complete = _injected\n", "INDETERMINATE", "incomplete"),
    ("stimulated_continuity = _injected\n", "INDETERMINATE", "injected defect"),
    ("stimulated_violations = _injected\n", "FAIL", "injected defect"),
    ("_real_stop = BoardWatch.stop\n\n\ndef _stop(self):\n    r = _real_stop(self)\n    r.epoch_changed = True\n    r.errors.append('server epoch changed on an event page (injected)')\n    return r\n\n\nBoardWatch.stop = _stop\n",
     "INDETERMINATE", "epoch changed"),
    ("_real_stop = BoardWatch.stop\n\n\ndef _stop(self):\n    r = _real_stop(self)\n    r.pages[0]['lost'] = 1\n    return r\n\n\nBoardWatch.stop = _stop\n",
     "INDETERMINATE", "lost")])
def test_reset_both_wiring_of_the_active_observation(sim, tmp_path, defect, verdict, fragment):
    url, client = sim(RESET_ENV)
    run, reports = run_with_defect(tmp_path, url, "test_m40_4_simultaneous_uart", reset_session(), INJECT + defect, extra=("--qual-allow-reset",))
    assert run.returncode != 0 and reports["M40-4"]["status"] == verdict, (defect[:30], run.stdout[-900:])
    assert fragment in run.stdout + " ".join(reports["M40-4"]["incidents"])


def assert_nothing_released_or_replayed(directory, client):
    rows = [json.loads(line) for path in directory.glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    operations = [r for r in rows if r["method"] == "POST" and r["path"] == "/api/v1/operations"]
    assert len(operations) == 2, f"{len(operations)} operations submitted: a stuck command must never be submitted again"
    assert not [r for r in rows if r["method"] == "DELETE" and r["path"].startswith("/api/v1/leases/")], "a reservation was released although an operation is unknown"
    assert all(client.device(name)["reservation"] is not None for name in ("board", "board2")), "the reservations must be retained"


def test_a_reset_that_never_reports_is_indeterminate_and_keeps_its_reservations(sim, tmp_path):
    """The simulated page agent never answers `reset`. The server gives the command up after its short timeout: the threads end with an
    unresolved outcome, the verdict is INDETERMINATE, nothing is released and nothing is submitted twice."""
    url, client = sim({**RESET_ENV, "SIM_HANG_COMMAND": "reset"})
    document = reset_session()
    document["operation_timeout_s"] = 3
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_4_simultaneous_uart", document, extra=("--qual-allow-reset",))
    assert run.returncode != 0 and reports["M40-4"]["status"] == "INDETERMINATE", run.stdout[-1500:]
    assert "indeterminate hardware outcome" in " ".join(reports["M40-4"]["incidents"]), "the thread's own unresolved outcome must be what is reported"
    assert_nothing_released_or_replayed(tmp_path / "evidence", client)


def test_a_command_thread_still_alive_at_the_join_deadline_is_indeterminate_and_not_released(sim, tmp_path):
    """The join deadline is forced to 0.5 s while the command is stuck for much longer: the thread is provably still running."""
    url, client = sim({**RESET_ENV, "SIM_HANG_COMMAND": "reset"})
    defect = ("\n_real_fire = fire_concurrently\n\n\ndef fire_concurrently(boards, command, *, join_timeout, **kwargs):\n"
              "    return _real_fire(boards, command, join_timeout=0.5, **kwargs)\n")
    run, reports = run_with_defect(tmp_path, url, "test_m40_4_simultaneous_uart", reset_session(), defect, extra=("--qual-allow-reset",))
    assert run.returncode != 0 and reports["M40-4"]["status"] == "INDETERMINATE", run.stdout[-1500:]
    assert "still running" in run.stdout + " ".join(reports["M40-4"]["incidents"])
    assert_nothing_released_or_replayed(tmp_path / "evidence", client)


# --- M40-11c/d: evidence survives FAIL / INDETERMINATE / BLOCKED, and never masks the error in flight --------------------------------------
ENV_11CD = {"SIM_FLASH_PRELOAD": json.dumps(PRELOAD), "SIM_CONSOLE_BOOT": json.dumps(BOOT), "SIM_UART_TAG": "1"}
SELECT_11CD = "test_m40_11c_11d_known_content_and_application_return"


def evidence_11cd(tmp_path):
    folder = next((tmp_path / "evidence").glob("M40-11cd-*"))
    return folder, json.loads((folder / "m40-11c-reads.json").read_text()), json.loads((folder / "m40-11d-markers.json").read_text())


def operations_posted(tmp_path):
    return [r for r in posts(tmp_path / "evidence") if r["path"] == "/api/v1/operations"]


def assert_no_extra_reset_or_replay(tmp_path, names):
    """Whatever failed: no `reset`, at most ONE console, and exactly one operation POST per command that appears in the log."""
    assert "reset" not in names and names.count("console") <= 1, names
    assert len(operations_posted(tmp_path)) == len(names), f"{len(operations_posted(tmp_path))} POSTs for {names}"


@pytest.mark.parametrize("address,expected_reads,expected_phase,expected_names", [
    (0, 0, "bootloader-header", ["flash.read", "console"]),
    (0x10000, 2, "table", ["flash.read", "flash.read", "partitions", "flash.read", "console"]),
    (0x120000, 6, "application-slots", None)])
def test_a_read_error_keeps_the_reads_already_done_and_is_a_fail(sim, tmp_path, address, expected_reads, expected_phase, expected_names):
    url, client = sim({**ENV_11CD, "SIM_FAIL_READ_AT": str(address)})
    run, reports, _ = run_recipe(tmp_path, url, SELECT_11CD, session())
    assert run.returncode != 0 and reports["M40-11cd"]["status"] == "FAIL", (reports, run.stdout[-900:])
    folder, reads_file, markers_file = evidence_11cd(tmp_path)
    assert (reads_file["phase"], reads_file["error"], len(reads_file["reads"])) == (expected_phase, "OperationError", expected_reads)
    assert markers_file["phase"] == expected_phase and markers_file["markers"] == []
    names = commands(tmp_path / "evidence")
    if expected_names:
        assert names == expected_names, names
    assert names[-1] == "console", "the card was not issued a console return after a failure in the loader phase: it must be"
    assert_no_extra_reset_or_replay(tmp_path, names)
    assert (folder / "m40-observed-card.jsonl").is_file() and client.device("board")["reservation"] is None


def test_a_continuity_loss_during_the_marker_search_is_indeterminate_and_keeps_everything_observed(sim, tmp_path):
    url, client = sim({**ENV_11CD, "SIM_REENUMERATE_AFTER_CONSOLE": "1"})
    run, reports, _ = run_recipe(tmp_path, url, SELECT_11CD, session())
    assert run.returncode != 0 and reports["M40-11cd"]["status"] == "INDETERMINATE", (reports, run.stdout[-900:])
    folder, reads_file, markers_file = evidence_11cd(tmp_path)
    assert reads_file["phase"] in ("console", "markers") and reads_file["error"] == "ContinuityError" and len(reads_file["reads"]) == 8
    assert markers_file["error"] == "ContinuityError"
    names = commands(tmp_path / "evidence")
    assert names.count("console") == 1 and len(names) == 10, "the console was issued once; no second console, no replay"
    assert_no_extra_reset_or_replay(tmp_path, names)


def test_a_console_command_that_fails_is_a_fail_not_retried_and_leaves_an_unknown_restoration(sim, tmp_path):
    url, client = sim({**ENV_11CD, "SIM_FAIL_COMMAND": "console"})
    run, reports, _ = run_recipe(tmp_path, url, SELECT_11CD, session())
    report = reports["M40-11cd"]
    assert run.returncode != 0 and report["status"] == "FAIL", (report, run.stdout[-900:])
    assert report["restoration"].startswith("UNKNOWN") and "console command did not complete" in " ".join(report["incidents"])
    folder, reads_file, markers_file = evidence_11cd(tmp_path)
    assert reads_file["phase"] == "console" and reads_file["error"] == "OperationError" and len(reads_file["reads"]) == 8
    names = commands(tmp_path / "evidence")
    assert names.count("console") == 1 and len(names) == 10, "a failed console is never retried"
    assert_no_extra_reset_or_replay(tmp_path, names)


def test_a_console_command_that_never_answers_is_indeterminate_and_keeps_the_reservation(sim, tmp_path):
    url, client = sim({**ENV_11CD, "SIM_HANG_COMMAND": "console"})
    defect = ("\nimport iobewi_bench.client as _client\n_client.Board.console = lambda self: self.command('console', timeout=3, start_timeout=3)\n")
    run, reports = run_with_defect(tmp_path, url, SELECT_11CD, session(), defect)
    report = reports["M40-11cd"]
    assert run.returncode != 0 and report["status"] == "INDETERMINATE", (report, run.stdout[-900:])
    assert report["restoration"].startswith("UNKNOWN")
    folder, reads_file, markers_file = evidence_11cd(tmp_path)
    assert reads_file["phase"] == "console" and reads_file["error"] == "PendingOperation" and len(reads_file["reads"]) == 8
    names = commands(tmp_path / "evidence")
    assert names.count("console") == 1 and len(names) == 10
    assert_no_extra_reset_or_replay(tmp_path, names)
    rows = [json.loads(line) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    assert not [r for r in rows if r["method"] == "DELETE" and r["path"].startswith("/api/v1/leases/")], "no reservation released under an unknown outcome"
    assert client.device("board")["reservation"] is not None


EVIDENCE_WRITE_FAILS = ("\nimport iobewi_bench.qualification as _q\n_real_write = _q.Campaign.write\n\n\n"
                        "def _write(self, filename, value):\n    if filename == 'm40-11c-reads.json':\n        raise OSError('disk full (injected)')\n"
                        "    return _real_write(self, filename, value)\n\n\n_q.Campaign.write = _write\n")


def test_an_evidence_write_failure_never_replaces_the_error_in_flight(sim, tmp_path):
    url, client = sim({**ENV_11CD, "SIM_FAIL_READ_AT": "65536"})
    run, reports = run_with_defect(tmp_path, url, SELECT_11CD, session(), EVIDENCE_WRITE_FAILS)
    report = reports["M40-11cd"]
    assert run.returncode != 0 and report["status"] == "FAIL", (report, run.stdout[-900:])
    incidents = " ".join(report["incidents"])
    assert "OperationError" in incidents, "the ORIGINAL error is the one reported"
    assert "m40-11c-reads.json not written (OSError)" in incidents
    assert "could not be written" not in run.stdout, "the evidence problem must not become the verdict while another error is in flight"
    assert_no_extra_reset_or_replay(tmp_path, commands(tmp_path / "evidence"))


def test_an_evidence_write_failure_on_an_otherwise_clean_run_is_never_a_pass(sim, tmp_path):
    url, client = sim(ENV_11CD)
    run, reports = run_with_defect(tmp_path, url, SELECT_11CD, session(), EVIDENCE_WRITE_FAILS)
    assert run.returncode != 0 and reports["M40-11cd"]["status"] == "FAIL"
    assert "could not be written" in run.stdout + " ".join(reports["M40-11cd"]["incidents"])
    assert "m40-11c-reads.json not written (OSError)" in " ".join(reports["M40-11cd"]["incidents"])
    assert_no_extra_reset_or_replay(tmp_path, commands(tmp_path / "evidence"))


@pytest.mark.parametrize("env,expected", [({}, "PASS"), ({"SIM_FAIL_READ_AT": "0"}, "FAIL"), ({"SIM_REENUMERATE_AFTER_CONSOLE": "1"}, "INDETERMINATE"),
                                           ({"SIM_FLASH_PRELOAD": "[]"}, "BLOCKED")])
def test_the_four_verdicts_stay_distinct_and_each_writes_its_evidence(sim, tmp_path, env, expected):
    url, client = sim({**ENV_11CD, **env})
    run, reports, _ = run_recipe(tmp_path, url, SELECT_11CD, session())
    assert reports["M40-11cd"]["status"] == expected, (expected, reports["M40-11cd"]["status"], run.stdout[-600:])
    folder, reads_file, markers_file = evidence_11cd(tmp_path)                       # the evidence exists in EVERY verdict
    assert reads_file["phase"] and "planned" in reads_file
    assert_no_extra_reset_or_replay(tmp_path, commands(tmp_path / "evidence"))


STOP_FAILS = ("\n_real_stop = BoardWatch.stop\n\n\ndef _stop(self):\n    _real_stop(self)\n    raise RuntimeError('collector boom (injected)')\n\n\n"
              "BoardWatch.stop = _stop\n")


def write_fails_for(*names):
    return ("\nimport iobewi_bench.qualification as _q\nimport qualification_campaign.m40 as _m40\n_real_write = _q.Campaign.write\n_real_evidence = _m40.write_evidence\n"
            f"_names = {list(names)!r}\n\n\ndef _write(self, filename, value):\n    if filename in _names:\n        raise OSError('disk full (injected)')\n"
            "    return _real_write(self, filename, value)\n\n\ndef _evidence(path, result):\n    if 'm40-observed-card.jsonl' in _names:\n        raise OSError('disk full (injected)')\n"
            "    return _real_evidence(path, result)\n\n\n_q.Campaign.write = _write\n_m40.write_evidence = _evidence\n")


def test_a_collector_error_never_masks_a_read_error_and_the_evidence_is_kept(sim, tmp_path):
    url, client = sim({**ENV_11CD, "SIM_FAIL_READ_AT": "65536"})
    run, reports = run_with_defect(tmp_path, url, SELECT_11CD, session(), STOP_FAILS)
    report = reports["M40-11cd"]
    assert run.returncode != 0 and report["status"] == "FAIL", (report, run.stdout[-900:])
    incidents = " ".join(report["incidents"])
    assert "OperationError" in incidents, "the ORIGINAL read error stays the verdict"
    assert "collector stop failed (RuntimeError)" in incidents and "collector boom" not in report["status"]
    folder, reads_file, markers_file = evidence_11cd(tmp_path)                         # both files survive the collector failure
    assert (reads_file["phase"], reads_file["error"], len(reads_file["reads"])) == ("table", "OperationError", 2)
    assert (folder / "m40-observed-card.jsonl").is_file(), "the partial observation is still written"
    names = commands(tmp_path / "evidence")
    assert names[-1] == "console"
    assert_no_extra_reset_or_replay(tmp_path, names)


def test_a_collector_error_keeps_a_continuity_loss_indeterminate(sim, tmp_path):
    url, client = sim({**ENV_11CD, "SIM_REENUMERATE_AFTER_CONSOLE": "1"})
    run, reports = run_with_defect(tmp_path, url, SELECT_11CD, session(), STOP_FAILS)
    assert reports["M40-11cd"]["status"] == "INDETERMINATE", (reports, run.stdout[-900:])
    assert "collector stop failed" in " ".join(reports["M40-11cd"]["incidents"])
    folder, reads_file, markers_file = evidence_11cd(tmp_path)
    assert reads_file["error"] == "ContinuityError" and len(reads_file["reads"]) == 8
    assert_no_extra_reset_or_replay(tmp_path, commands(tmp_path / "evidence"))


def test_a_collector_error_on_an_otherwise_clean_run_is_an_incomplete_observation_not_a_pass(sim, tmp_path):
    url, client = sim(ENV_11CD)
    run, reports = run_with_defect(tmp_path, url, SELECT_11CD, session(), STOP_FAILS)
    report = reports["M40-11cd"]
    assert run.returncode != 0 and report["status"] == "INDETERMINATE", (report, run.stdout[-900:])
    assert "collector stop failed" in " ".join(report["incidents"]) and "incomplete" in run.stdout + " ".join(report["incidents"])
    folder, reads_file, markers_file = evidence_11cd(tmp_path)                         # the run's own evidence is complete
    assert reads_file["phase"] == "markers" and len(reads_file["reads"]) == 8 and markers_file["problems"] == []


@pytest.mark.parametrize("failing,kept", [
    (["m40-observed-card.jsonl"], ["m40-11c-reads.json", "m40-11d-markers.json"]),
    (["m40-11c-reads.json"], ["m40-observed-card.jsonl", "m40-11d-markers.json"]),
    (["m40-11d-markers.json"], ["m40-observed-card.jsonl", "m40-11c-reads.json"]),
    (["m40-observed-card.jsonl", "m40-11c-reads.json"], ["m40-11d-markers.json"])])
def test_each_evidence_file_is_written_independently_of_the_others(sim, tmp_path, failing, kept):
    """With a read error in flight (so the original error must stay the verdict) the failing writers are reported one by one and every
    other file is still written."""
    url, client = sim({**ENV_11CD, "SIM_FAIL_READ_AT": "65536"})
    run, reports = run_with_defect(tmp_path, url, SELECT_11CD, session(), write_fails_for(*failing))
    report = reports["M40-11cd"]
    assert run.returncode != 0 and report["status"] == "FAIL" and "OperationError" in " ".join(report["incidents"]), (report, run.stdout[-900:])
    folder = next((tmp_path / "evidence").glob("M40-11cd-*"))
    incidents = " ".join(report["incidents"])
    for name in failing:
        assert f"evidence {name} not written (OSError)" in incidents and not (folder / name).exists(), name
    for name in kept:
        assert (folder / name).is_file(), f"{name} must survive the failure of {failing}"
    assert "could not be written" not in run.stdout, "the persistence failure must not become the verdict while an error is in flight"
    assert_no_extra_reset_or_replay(tmp_path, commands(tmp_path / "evidence"))


def test_an_unwritten_file_on_a_clean_run_is_still_never_a_pass_but_the_others_are_kept(sim, tmp_path):
    url, client = sim(ENV_11CD)
    run, reports = run_with_defect(tmp_path, url, SELECT_11CD, session(), write_fails_for("m40-observed-card.jsonl"))
    assert reports["M40-11cd"]["status"] == "FAIL" and "could not be written" in run.stdout + " ".join(reports["M40-11cd"]["incidents"])
    folder = next((tmp_path / "evidence").glob("M40-11cd-*"))
    assert (folder / "m40-11c-reads.json").is_file() and (folder / "m40-11d-markers.json").is_file()


def test_the_dumps_are_restricted_even_when_every_evidence_file_fails_to_write(sim, tmp_path):
    """The permission fix of the dumps is its own writer: it must not depend on any evidence file being writable."""
    url, client = sim({**ENV_11CD, "SIM_FAIL_READ_AT": "65536"})
    run, reports = run_with_defect(tmp_path, url, SELECT_11CD, session(),
                                   write_fails_for("m40-observed-card.jsonl", "m40-11c-reads.json", "m40-11d-markers.json"))
    assert reports["M40-11cd"]["status"] == "FAIL" and "OperationError" in " ".join(reports["M40-11cd"]["incidents"])
    folder = next((tmp_path / "evidence").glob("M40-11cd-*"))
    dumps = list((folder / "board-0").glob("flash-*"))
    assert dumps, "the reads done before the failure left private dumps"
    assert all((path.stat().st_mode & 0o777) == 0o600 for path in dumps), [oct(p.stat().st_mode & 0o777) for p in dumps]
    assert not any((folder / name).exists() for name in ("m40-observed-card.jsonl", "m40-11c-reads.json", "m40-11d-markers.json"))

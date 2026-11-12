"""Software checks of P4-ROM-MD5 (ROM loader without stub). SIMULATION ONLY: a RAM simulator that DECLARES itself an esp32p4 with 32 MiB and mimics the agent's `loader: "rom"`
contract (ROM answer format, loader_stub 0, identification rank per session). It proves the recipe's decisions and evidence, never the behaviour of the real ROM, esptool-js or the flash."""
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

import pytest

from iobewi_bench import BenchClient

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
sys.path.insert(0, str(BENCH))
from protocol import Fault, validate_command                                       # noqa: E402
from qualification_campaign.rom_md5 import analyse_rom                             # noqa: E402
from qualification_campaign.session import SessionError, validate_session          # noqa: E402

RECIPE = "recipes/qualification_campaign/test_pc_rom_md5.py"
MIB = 1024 * 1024
ASSETS = "ab" * 32
SCENARIO = "P4-ROM-MD5"
BASE_ENV = {"SIM_FLASH_MIB": "32", "SIM_FLASH_PATTERN": "sector-id", "SIM_JEDEC": "1", "SIM_ASSETS_SHA256": ASSETS, "SIM_IDENTIFY_EVENTS": "1", "SIM_CONSOLE_EVENT": "1"}
H = lambda c: c * 32      # noqa: E731
DEVICE = {"capabilities": ["identify", "flash.read", "flash.md5"], "flash_size": 32 * MIB, "chip": "esp32p4"}


def valid(**change):
    base = {"version": 1, "board_id": "board", "chip": "esp32p4", "flash_size": 32 * MIB, "transport": "external_uart",
            "partition_table_offset": None, "evidence_root": "/private/evidence", "accept_unknown_transport": True}
    return {**base, **change}


# --- session, protocol and analysis (pure) ------------------------------------------------------------------------------------
def test_the_scenario_is_known_and_its_consent_is_separate_from_the_other_integrity_scenarios():
    assert validate_session(valid(consent={"scenarios": [SCENARIO]}))["consent"]["scenarios"] == [SCENARIO]
    assert validate_session(valid())["consent"]["scenarios"] == []


def test_the_loader_argument_is_a_strict_choice_on_flash_md5_only():
    base = {"address": 0x2000, "size": 4096}
    validate_command("flash.md5", base, DEVICE, {})
    validate_command("flash.md5", {**base, "loader": "stub"}, DEVICE, {})
    validate_command("flash.md5", {**base, "loader": "rom", "return_console": False}, DEVICE, {})
    for bad in ("ROM", "none", "", 1, None, True):
        with pytest.raises(Fault):
            validate_command("flash.md5", {**base, "loader": bad}, DEVICE, {})
    with pytest.raises(Fault):
        validate_command("flash.read", {**base, "loader": "rom"}, DEVICE, {})
    with pytest.raises(Fault):
        validate_command("identify", {"loader": "rom"}, DEVICE, {})


def rom(md5, index, stub=0, fmt="rom-ascii32", loader="rom", ops=0):
    return {"kind": "rom_md5", "index": index, "md5": md5, "format": fmt, "loader": loader, "ram_load_ops": ops, "identify": {"loader_stub": stub, "identify_index": 1}}


def test_three_identical_rom_md5_are_row_a_and_consistent():
    r = analyse_rom([rom(H("a"), 0), rom(H("a"), 1), rom(H("a"), 2)])
    assert r["conclusion"] == "consistent" and r["rows"] == ["ROM-A"]
    assert all(f["level"] == "hypothesis" for f in r["findings"] if f["id"].startswith("ROM-"))
    assert "No cause is established" in r["caution"]


def test_the_two_md5_of_one_session_differing_is_row_b():
    r = analyse_rom([rom(H("a"), 0), rom(H("b"), 1)])
    assert (r["conclusion"], r["rows"]) == ("discordance", ["ROM-B"])


def test_a_new_entry_that_differs_is_row_c_and_a_missing_third_is_incomplete():
    assert analyse_rom([rom(H("a"), 0), rom(H("a"), 1), rom(H("b"), 2)])["rows"] == ["ROM-C"]
    r = analyse_rom([rom(H("a"), 0), rom(H("a"), 1)])
    assert (r["conclusion"], r["rows"]) == ("incomplete", ["ROM-D"])


@pytest.mark.parametrize("bad", [rom("", 0), rom(H("a"), 0, stub=1), rom(H("a"), 0, fmt="stub-raw16"), rom(H("a"), 0, loader="stub"), rom("A" * 32, 0), rom(H("a"), 0, ops=5)])
def test_an_empty_malformed_or_non_rom_answer_is_never_a_success(bad):
    r = analyse_rom([bad, rom(H("a"), 1), rom(H("a"), 2)])
    assert r["conclusion"] == "incomplete" and r["rows"] == ["ROM-D"]


def test_fewer_than_two_measurements_is_incomplete():
    assert analyse_rom([])["conclusion"] == "incomplete"
    assert analyse_rom([rom(H("a"), 0)])["conclusion"] == "incomplete"


# --- simulated bench ------------------------------------------------------------------------------------------------------
@pytest.fixture
def sim():
    started = []

    def start(env=None):
        process = subprocess.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--chip", "esp32p4"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env={**os.environ, **BASE_ENV, **(env or {})})
        started.append(process)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10), "simulated server did not start"
        url = json.loads(process.stdout.readline())["url"]
        client = BenchClient(url, "pytest-client-test-token")
        deadline = time.monotonic() + 10
        while not client.devices():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return url, client
    yield start
    for process in started:
        process.terminate()
        process.communicate(timeout=10)


def run_recipe(tmp_path, url, session=None, *flags, ephemeral_ok=True):
    document = {**valid(), "evidence_root": str(tmp_path / "evidence"), "operator_timeout_s": 40, "operation_timeout_s": 60,
                "consent": {"scenarios": [SCENARIO]}, "integrity": {"expected_assets_sha256": ASSETS}, **(session or {})}
    (tmp_path / "session.json").write_text(json.dumps(document))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json"),
           "PYTHONPATH": str(BENCH / "python/src")}
    run = subprocess.run([sys.executable, "-m", "pytest", RECIPE, "--bench", "--qual-allow-dirty", *(["--qual-allow-ephemeral-evidence"] if ephemeral_ok else []), *flags,
                          "--bench-results", str(tmp_path / "runs"), "-q", "-rs", "-p", "no:cacheprovider", "--tb=short"], cwd=BENCH, env=env, capture_output=True,
                         text=True, timeout=240)
    reports = {}
    for path in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(path.read_text())["tests"]:
            reports[item["id"]] = item
    return run, reports


def case_dir(tmp_path):
    return next((tmp_path / "evidence").glob("PC-9-*"))


def commands(tmp_path):
    return [json.loads(line) for line in (case_dir(tmp_path) / "board-0" / "commands.jsonl").read_text().splitlines()]


def submitted(tmp_path):
    rows = [json.loads(line) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows if r["method"] == "POST" and r["path"] == "/api/v1/operations"]


def analysis(tmp_path):
    return json.loads((case_dir(tmp_path) / "rom-analysis.json").read_text())


def test_stable_rom_md5_pass_with_three_rom_commands_and_no_flash_read(sim, tmp_path):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-9"]["status"] == "PASS", run.stdout[-1800:] + run.stderr[-300:]
    cmds = commands(tmp_path)
    assert [c["command"] for c in cmds] == ["flash.md5"] * 3, "ROM MD5 only: no identify, no flash.read, no explicit console"
    assert [c["args"] for c in cmds] == [{"address": 0x2000, "size": 4096, "return_console": rc, "loader": "rom"} for rc in (False, True, True)]
    evidence = " ".join(reports["PC-9"]["evidence"])
    assert "identification ranks: [1, 2, 1]" in evidence and "format=rom-ascii32" in evidence and "loader_stub=0" in evidence and "ram_load_ops=0" in evidence
    assert analysis(tmp_path)["conclusion"] == "consistent"
    assert client.artifacts()["artifacts"] == [] and client.device("board")["reservation"] is None


def test_the_two_md5_of_one_session_differing_stops_before_the_third(sim, tmp_path):
    url, _ = sim({"SIM_ROM_MODE": "vary"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-9"]["status"] == "FAIL"
    assert analysis(tmp_path)["rows"] == ["ROM-B"]
    assert [c["command"] for c in commands(tmp_path)] == ["flash.md5", "flash.md5"], "no third measurement"
    assert len(submitted(tmp_path)) == 2


def test_a_third_measurement_that_differs_after_a_new_entry_is_row_c(sim, tmp_path):
    url, _ = sim({"SIM_ROM_MODE": "vary_third"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-9"]["status"] == "FAIL" and analysis(tmp_path)["rows"] == ["ROM-C"]
    assert len(submitted(tmp_path)) == 3


def test_an_identification_failure_at_the_second_command_keeps_the_raw_value_and_stops(sim, tmp_path):
    url, client = sim({"SIM_JEDEC_INVALID_AT": "2", "SIM_JEDEC_INVALID_VALUE": "4286513152"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-9"]["status"] == "FAIL"
    failure = json.loads(next(case_dir(tmp_path).rglob("1-rom_md5-failure.json")).read_text())["error"]
    assert (failure["code"], failure["phase"], failure["jedec_id"], failure["identify_index"], failure["loader_stub"]) == ("flash_capacity_unknown", "identify", 4286513152, 2, 0)
    assert [c["command"] for c in commands(tmp_path)] == ["flash.md5", "flash.md5", "console"], "stops, one console return after the early exit, no retry"
    assert client.device("board")["reservation"] is None and not client.device("board")["quarantined"]


def test_an_agent_that_loaded_the_stub_anyway_is_detected_at_once(sim, tmp_path):
    url, _ = sim({"SIM_ROM_STUB_LEAK": "1"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-9"]["status"] == "FAIL"
    assert "not a ROM answer without stub" in " ".join(reports["PC-9"]["incidents"] + reports["PC-9"]["evidence"] + [run.stdout])
    assert [c["command"] for c in commands(tmp_path)] == ["flash.md5", "console"], "nothing after the first measurement but the console return"


@pytest.mark.parametrize("env,code", [({"SIM_ROM_STUB_RUNNING": "1"}, "rom_mode_stub_running"), ({"SIM_ROM_ATTACH_FAIL": "1"}, "rom_flash_attach_failed")])
def test_a_rom_session_that_cannot_start_is_a_fail_without_measurement(sim, tmp_path, env, code):
    url, _ = sim(env)
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-9"]["status"] == "FAIL"
    assert json.loads(next(case_dir(tmp_path).rglob("0-rom_md5-failure.json")).read_text())["error"]["code"] == code
    assert not list(case_dir(tmp_path).glob("rom-analysis.json"))


def test_an_invalid_rom_answer_is_a_fail_with_no_md5(sim, tmp_path):
    url, _ = sim({"SIM_MD5_INVALID": "1"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-9"]["status"] == "FAIL"
    assert json.loads(next(case_dir(tmp_path).rglob("0-rom_md5-failure.json")).read_text())["error"]["code"] == "md5_response_invalid"


def test_the_other_integrity_consents_do_not_cover_this_scenario(sim, tmp_path):
    url, _ = sim()
    run, _ = run_recipe(tmp_path, url, {"consent": {"scenarios": ["P4-FLASH-INTEGRITY", "P4-FLASH-INTEGRITY-SESSION"]}})
    assert "BLOCKED" in run.stdout and not submitted(tmp_path)


def test_wrong_assets_target_or_storage_block_before_any_command(sim, tmp_path):
    url, _ = sim({"SIM_ASSETS_SHA256": "cd" * 32})
    run, reports = run_recipe(tmp_path, url)
    assert reports.get("PC-9", {}).get("status") == "BLOCKED" and not submitted(tmp_path)
    other = tmp_path / "c6"
    other.mkdir()
    url2, _ = sim()
    run, _ = run_recipe(other, url2, {"chip": "esp32c6", "flash_size": 8 * MIB})
    assert "BLOCKED" in run.stdout
    from qualification_campaign.evidence import check_persistent_root
    if not check_persistent_root(str(tmp_path / "x"))[0]:
        third = tmp_path / "eph"
        third.mkdir()
        url3, _ = sim()
        run, _ = run_recipe(third, url3, None, ephemeral_ok=False)
        assert "BLOCKED" in run.stdout and not submitted(third)

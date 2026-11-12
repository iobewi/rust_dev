"""Software checks of the P4-JEDEC-READ recipe (G1). SIMULATION ONLY: a RAM simulator that DECLARES itself an esp32p4 with 32 MiB and that mimics the
instrumented agent's contract (raw JEDEC diagnostics per identification, `flash_capacity_unknown` on an invalid id). It proves the recipe's decisions and
evidence, never the behaviour of the real P4, the loader or esptool-js."""
import hashlib
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
from qualification_campaign.session import SessionError, validate_session          # noqa: E402

RECIPE = "recipes/qualification_campaign/test_pc_jedec_read.py"
MIB = 1024 * 1024
ASSETS = "ab" * 32
BASE_ENV = {"SIM_FLASH_MIB": "32", "SIM_FLASH_PATTERN": "sector-id", "SIM_JEDEC": "1", "SIM_ASSETS_SHA256": ASSETS,
            "SIM_IDENTIFY_EVENTS": "1", "SIM_CONSOLE_EVENT": "1"}      # the console-return events the real agent emits


def valid(**change):
    base = {"version": 1, "board_id": "board", "chip": "esp32p4", "flash_size": 32 * MIB, "transport": "external_uart",
            "partition_table_offset": None, "evidence_root": "/private/evidence", "accept_unknown_transport": True}
    return {**base, **change}


# --- session ----------------------------------------------------------------------------------------------------------
def test_the_jedec_section_accepts_only_a_lowercase_sha256_digest():
    assert validate_session(valid(jedec={"expected_assets_sha256": ASSETS}))["jedec"] == {"expected_assets_sha256": ASSETS}
    assert validate_session(valid())["jedec"] == {}
    for bad in ({"expected_assets_sha256": "A" * 64}, {"expected_assets_sha256": "a" * 63}, {"expected_assets_sha256": 5}, {"unknown": 1}, "x"):
        with pytest.raises(SessionError):
            validate_session(valid(jedec=bad))


def test_the_scenario_is_known_but_never_granted_by_default():
    cfg = validate_session(valid(consent={"scenarios": ["P4-JEDEC-READ"]}))
    assert cfg["consent"]["scenarios"] == ["P4-JEDEC-READ"]
    assert validate_session(valid())["consent"]["scenarios"] == []


# --- simulated bench ----------------------------------------------------------------------------------------------------
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


def run_recipe(tmp_path, url, session=None, *flags):
    document = {**valid(), "evidence_root": str(tmp_path / "evidence"), "operator_timeout_s": 40, "operation_timeout_s": 60,
                "consent": {"scenarios": ["P4-JEDEC-READ"]}, "jedec": {"expected_assets_sha256": ASSETS}, **(session or {})}
    (tmp_path / "session.json").write_text(json.dumps(document))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json")}
    run = subprocess.run([sys.executable, "-m", "pytest", RECIPE, "--bench", "--qual-allow-dirty", *flags, "--bench-results", str(tmp_path / "runs"),
                          "-q", "-rs", "-p", "no:cacheprovider", "--tb=short"], cwd=BENCH, env=env, capture_output=True, text=True, timeout=240)
    reports = {}
    for path in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(path.read_text())["tests"]:
            reports[item["id"]] = item
    return run, reports


def case_dir(tmp_path):
    return next((tmp_path / "evidence").glob("PC-6-*"))


def commands(tmp_path):
    return [json.loads(line) for line in (case_dir(tmp_path) / "board-0" / "commands.jsonl").read_text().splitlines()]


def submitted(tmp_path):
    rows = [json.loads(line) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows if r["method"] == "POST" and r["path"] == "/api/v1/operations"]


# --- the recipe --------------------------------------------------------------------------------------------------------------
def test_two_reads_with_console_return_pass_and_keep_the_jedec_of_every_identification(sim, tmp_path):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-6"]["status"] == "PASS", run.stdout[-1500:] + run.stderr[-300:]
    directory = case_dir(tmp_path)
    cmds = commands(tmp_path)
    assert [c["command"] for c in cmds] == ["identify", "flash.read", "flash.read"], "read only, exactly three operations"
    assert [c["args"] for c in cmds[1:]] == [{"address": 0x2000, "return_console": True, "size": 4096}] * 2
    first = json.loads((directory / "read-1.json").read_text())
    second = json.loads((directory / "read-2.json").read_text())
    assert first["sha256"] == second["sha256"] and first["size"] == second["size"] == 4096
    expected = hashlib.sha256((0x2000 // 4096).to_bytes(4, "little") * 1024).hexdigest()
    assert first["sha256"] == expected, "the bytes at 0x2000 are the sector with index 2"
    assert first["identify"] == {"jedec_id": 0x1940C8, "identify_index": 1, "loader_stub": 1, "chip_description": "ESP32-P4 (revision v1.0)"}
    assert second["identify"]["identify_index"] == 1, "return_console=true: every read opens a new loader session"
    versions = json.loads((directory / "versions.json").read_text())
    assert versions["page_assets_sha256"] == ASSETS == versions["expected_assets_sha256"]
    lines = " ".join(reports["PC-6"]["evidence"])
    assert "jedec_id=0x1940c8" in lines and "identify: chip=esp32p4" in lines
    assert client.artifacts()["artifacts"] == [] and client.device("board")["reservation"] is None


def test_an_invalid_jedec_on_the_second_read_is_a_fail_with_the_raw_value_and_no_retry(sim, tmp_path):
    url, client = sim({"SIM_JEDEC_INVALID_AT": "2"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-6"]["status"] == "FAIL", run.stdout[-1500:]
    failure = json.loads(next(case_dir(tmp_path).rglob("read-2-failure.json")).read_text())
    assert failure["error"]["code"] == "flash_capacity_unknown"
    assert (failure["error"]["jedec_id"], failure["error"]["phase"], failure["error"]["identify_index"], failure["error"]["loader_stub"]) == (0, "identify", 1, 1)
    assert "jedec_id=0 " in " ".join(reports["PC-6"]["evidence"])
    assert len(submitted(tmp_path)) == 3, "identify, read 1, read 2: the failed read is not repeated, no third read"
    assert client.device("board")["reservation"] is None and not client.device("board")["quarantined"]


def test_an_invalid_jedec_on_the_first_read_stops_before_the_second(sim, tmp_path):
    url, client = sim({"SIM_JEDEC_INVALID_AT": "1"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-6"]["status"] == "FAIL", run.stdout[-1200:]
    assert list(case_dir(tmp_path).rglob("read-1-failure.json")) and not list(case_dir(tmp_path).rglob("read-2*.json"))
    assert len(submitted(tmp_path)) == 2


def test_an_agent_that_does_not_report_the_diagnostics_is_a_fail(sim, tmp_path):
    url, client = sim({"SIM_JEDEC": "0"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-6"]["status"] == "FAIL" and "did not report the JEDEC diagnostics" in " ".join(reports["PC-6"]["incidents"])
    assert len(submitted(tmp_path)) == 2, "stops after the first read"


@pytest.mark.parametrize("env,session,why", [
    ({"SIM_ASSETS_SHA256": "cd" * 32}, {}, "not the reviewed instrumentation"),                      # another agent version is running
    ({}, {"jedec": {}}, "jedec.expected_assets_sha256 is not declared"),
    ({}, {"consent": {"scenarios": []}}, "P4-JEDEC-READ"),
    ({}, {"flash_size": 16 * MIB}, "reads the esp32p4 with 33554432 bytes")])
def test_the_scenario_is_blocked_before_any_command(sim, tmp_path, env, session, why):
    url, client = sim(env)
    run, reports = run_recipe(tmp_path, url, session)
    assert "BLOCKED" in run.stdout and why in (run.stdout + json.dumps(reports)), run.stdout[-900:]
    assert reports.get("PC-6", {"status": "BLOCKED"})["status"] == "BLOCKED"
    assert not submitted(tmp_path), "no operation may be submitted when a precondition is missing"


def test_a_c6_session_is_blocked(sim, tmp_path):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url, {"chip": "esp32c6", "flash_size": 8 * MIB})
    assert "BLOCKED" in run.stdout and "reads the esp32p4" in run.stdout and not submitted(tmp_path)

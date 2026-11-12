"""Software checks of P4-FLASH-INTEGRITY-SESSION (one loader session). SIMULATION ONLY: a RAM simulator that DECLARES itself an esp32p4 with 32 MiB; it models the
identification rank of the loader session and an invalid identification at the N-th flash command. It proves the recipe's decisions and evidence, never the
behaviour of the real P4, its stub or its flash."""
import json
import os
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from iobewi_bench import BenchClient

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
from qualification_campaign.integrity import PLAN_SESSION, analyse                  # noqa: E402
from qualification_campaign.session import SessionError, validate_session          # noqa: E402

RECIPE = "recipes/qualification_campaign/test_pc_integrity_session.py"
MIB = 1024 * 1024
ASSETS = "ab" * 32
BASE_ENV = {"SIM_FLASH_MIB": "32", "SIM_FLASH_PATTERN": "sector-id", "SIM_JEDEC": "1", "SIM_ASSETS_SHA256": ASSETS, "SIM_IDENTIFY_EVENTS": "1", "SIM_CONSOLE_EVENT": "1"}
SCENARIO = "P4-FLASH-INTEGRITY-SESSION"
H = lambda c: c * 32      # noqa: E731


def valid(**change):
    base = {"version": 1, "board_id": "board", "chip": "esp32p4", "flash_size": 32 * MIB, "transport": "external_uart",
            "partition_table_offset": None, "evidence_root": "/private/evidence", "accept_unknown_transport": True}
    return {**base, **change}


# --- session and analysis (pure) ------------------------------------------------------------------------------------------------
def test_the_scenario_is_known_and_its_consent_is_separate_from_pc7():
    assert validate_session(valid(consent={"scenarios": [SCENARIO]}))["consent"]["scenarios"] == [SCENARIO]
    assert validate_session(valid())["consent"]["scenarios"] == []


def rows(chip=("a", "a"), client="a", stub="a", state="received"):
    return [{"kind": "chip_md5", "index": 0, "md5": H(chip[0])},
            {"kind": "read", "index": 1, "md5_client": H(client), "stub": {"state": state, **({"md5": H(stub)} if state == "received" else {})}},
            {"kind": "chip_md5", "index": 2, "md5": H(chip[1])}]


def test_the_session_plan_is_three_measurements_and_agreement_is_consistent():
    assert PLAN_SESSION == ("chip_md5", "read", "chip_md5")
    assert analyse(rows(), PLAN_SESSION)["conclusion"] == "consistent"
    assert analyse(rows(), PLAN_SESSION)["rows"] == ["R4"]


def test_a_short_session_plan_is_incomplete_and_the_default_plan_still_needs_five():
    assert analyse(rows()[:2], PLAN_SESSION)["conclusion"] == "incomplete"
    assert analyse(rows())["conclusion"] == "incomplete", "three measurements do not satisfy the five-measurement plan"


def test_chip_md5_varying_inside_the_session_is_a_discordance_without_a_named_cause():
    result = analyse(rows(chip=("a", "f")), PLAN_SESSION)
    assert result["conclusion"] == "discordance" and result["flags"]["chip_md5_varies"]
    assert "No cause is established" in result["caution"]


# --- simulated bench ---------------------------------------------------------------------------------------------------------
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
    return next((tmp_path / "evidence").glob("PC-8-*"))


def commands(tmp_path):
    return [json.loads(line) for line in (case_dir(tmp_path) / "board-0" / "commands.jsonl").read_text().splitlines()]


def submitted(tmp_path):
    rows_ = [json.loads(line) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows_ if r["method"] == "POST" and r["path"] == "/api/v1/operations"]


def test_one_continuous_session_passes_with_md5_read_md5_and_one_final_console(sim, tmp_path):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-8"]["status"] == "PASS", run.stdout[-1800:] + run.stderr[-300:]
    cmds = commands(tmp_path)
    assert [c["command"] for c in cmds] == ["flash.md5", "flash.read", "flash.md5", "console"], "no separate identify, one explicit console at the end"
    assert all(c["args"]["return_console"] is False for c in cmds[:3]), "the loader is kept between the three commands"
    assert cmds[1]["args"]["stub_digest"] is True
    evidence = " ".join(reports["PC-8"]["evidence"])
    assert "identification ranks in the loader session: [1, 2, 3]" in evidence and "console-opened events before the explicit return: 0" in evidence
    assert "does not clear the variation seen between sessions" in evidence
    assert client.artifacts()["artifacts"] == [] and client.device("board")["reservation"] is None
    copy = next((case_dir(tmp_path) / "board-0").glob("flash-*-00002000.bin"))
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600 and len(copy.read_bytes()) == 4096


def test_an_invalid_identification_at_the_second_command_is_the_measurement_and_stops_the_sequence(sim, tmp_path):
    url, client = sim({"SIM_JEDEC_INVALID_AT": "2"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-8"]["status"] == "FAIL", run.stdout[-1500:]
    failure = json.loads(next(case_dir(tmp_path).rglob("1-read-failure.json")).read_text())
    assert failure["error"]["code"] == "flash_capacity_unknown"
    assert (failure["error"]["jedec_id"], failure["error"]["phase"], failure["error"]["identify_index"]) == (0, "identify", 2)
    assert [c["command"] for c in commands(tmp_path)] == ["flash.md5", "flash.read", "console"], "no third measurement, no retry, one console return"
    assert len(submitted(tmp_path)) == 3
    assert client.device("board")["reservation"] is None and not client.device("board")["quarantined"] and client.artifacts()["artifacts"] == []


def test_a_session_that_is_not_continuous_is_a_fail_even_if_the_digests_agree(sim, tmp_path):
    url, _ = sim({"SIM_SESSION_RESET_EACH": "1"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-8"]["status"] == "FAIL"
    assert "identification ranks in the loader session: [1, 1, 1]" in " ".join(reports["PC-8"]["evidence"])
    assert "the loader session was not continuous" in " ".join(reports["PC-8"]["incidents"])
    assert json.loads((case_dir(tmp_path) / "integrity-analysis.json").read_text())["conclusion"] == "consistent", "digests agree: only the session proof fails"


def test_corrupted_received_bytes_with_a_stable_stub_digest_fail(sim, tmp_path):
    url, _ = sim({"SIM_INTEGRITY": "transport"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-8"]["status"] == "FAIL"
    analysis = json.loads((case_dir(tmp_path) / "integrity-analysis.json").read_text())
    assert analysis["conclusion"] == "discordance" and analysis["flags"]["stub_md5_differs_from_received"] == [1]


@pytest.mark.parametrize("state", ["absent", "partial", "invalid"])
def test_a_missing_final_digest_is_incomplete_and_fails(sim, tmp_path, state):
    url, _ = sim({"SIM_STUB_DIGEST_STATE": state})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-8"]["status"] == "FAIL"
    assert json.loads((case_dir(tmp_path) / "integrity-analysis.json").read_text())["conclusion"] == "incomplete"


def test_the_pc7_consent_does_not_cover_this_scenario(sim, tmp_path):
    url, _ = sim()
    run, reports = run_recipe(tmp_path, url, {"consent": {"scenarios": ["P4-FLASH-INTEGRITY"]}})
    assert "BLOCKED" in run.stdout and not submitted(tmp_path)


def test_wrong_assets_or_target_or_storage_block_before_any_command(sim, tmp_path):
    url, _ = sim({"SIM_ASSETS_SHA256": "cd" * 32})
    run, reports = run_recipe(tmp_path, url)
    assert reports.get("PC-8", {}).get("status") == "BLOCKED" and not submitted(tmp_path)
    url2, _ = sim()
    other = tmp_path / "c6"
    other.mkdir()
    run, _ = run_recipe(other, url2, {"chip": "esp32c6", "flash_size": 8 * MIB})
    assert "BLOCKED" in run.stdout

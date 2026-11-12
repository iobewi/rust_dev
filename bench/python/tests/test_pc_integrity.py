"""Software checks of the P4-FLASH-INTEGRITY recipe and of its pure analysis (G1). SIMULATION ONLY: a RAM simulator that DECLARES itself an esp32p4 with 32 MiB
and mimics the agent's flash.md5 / stub_digest contract. It proves decisions and evidence, never the behaviour of the real P4, the stub or esptool-js."""
import hashlib
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
from qualification_campaign.integrity import analyse                               # noqa: E402
from qualification_campaign.session import SessionError, validate_session          # noqa: E402

RECIPE = "recipes/qualification_campaign/test_pc_integrity.py"
MIB = 1024 * 1024
ASSETS = "ab" * 32
BASE_ENV = {"SIM_FLASH_MIB": "32", "SIM_FLASH_PATTERN": "sector-id", "SIM_JEDEC": "1", "SIM_ASSETS_SHA256": ASSETS,
            "SIM_IDENTIFY_EVENTS": "1", "SIM_CONSOLE_EVENT": "1"}
H = lambda c: c * 32      # noqa: E731


def valid(**change):
    base = {"version": 1, "board_id": "board", "chip": "esp32p4", "flash_size": 32 * MIB, "transport": "external_uart",
            "partition_table_offset": None, "evidence_root": "/private/evidence", "accept_unknown_transport": True}
    return {**base, **change}


# --- session ----------------------------------------------------------------------------------------------------------
def test_the_integrity_section_accepts_only_a_lowercase_sha256_digest():
    assert validate_session(valid(integrity={"expected_assets_sha256": ASSETS}))["integrity"] == {"expected_assets_sha256": ASSETS}
    assert validate_session(valid())["integrity"] == {}
    for bad in ({"expected_assets_sha256": "A" * 64}, {"expected_assets_sha256": "a" * 63}, {"expected_assets_sha256": 5}, {"unknown": 1}, "x"):
        with pytest.raises(SessionError):
            validate_session(valid(integrity=bad))


def test_the_scenario_is_never_granted_by_default():
    assert validate_session(valid())["consent"]["scenarios"] == []
    assert validate_session(valid(consent={"scenarios": ["P4-FLASH-INTEGRITY"]}))["consent"]["scenarios"] == ["P4-FLASH-INTEGRITY"]


# --- pure analysis ----------------------------------------------------------------------------------------------------------
def obs(chip=("a", "a", "a"), client=("a", "a"), stub=("a", "a"), state="received"):
    rows = []
    c, r = iter(chip), iter(zip(client, stub))
    for index, kind in enumerate(("chip_md5", "read", "chip_md5", "read", "chip_md5")):
        if kind == "chip_md5":
            rows.append({"kind": kind, "index": index, "md5": H(next(c))})
        else:
            cl, st = next(r)
            rows.append({"kind": kind, "index": index, "md5_client": H(cl), "stub": {"state": state, **({"md5": H(st)} if state == "received" else {})}})
    return rows


def test_all_digests_agree_is_consistent_and_only_about_this_observation():
    result = analyse(obs())
    assert result["conclusion"] == "consistent" and result["rows"] == ["R4"] and result["complete"]


def test_a_stable_stub_digest_with_varying_received_bytes_points_at_the_transfer_as_a_hypothesis():
    result = analyse(obs(client=("a", "b"), stub=("a", "a"), chip=("a", "a", "a")))
    assert result["conclusion"] == "discordance" and "R1" in result["rows"]
    assert all(f["level"] == "hypothesis" for f in result["findings"] if f["id"].startswith("R"))
    assert "No cause is established" in result["caution"]


def test_a_varying_stub_digest_is_row_r2():
    result = analyse(obs(client=("a", "b"), stub=("a", "b"), chip=("a", "a", "a")))
    assert "R2" in result["rows"] and result["flags"]["stub_md5_varies"]


def test_stable_chip_md5_with_varying_reads_is_row_r3():
    assert "R3" in analyse(obs(client=("a", "b"), stub=("a", "a"), chip=("a", "a", "a")))["rows"]


def test_a_single_discordance_never_concludes_consistent():
    assert analyse(obs(chip=("a", "f", "a")))["conclusion"] == "discordance"
    assert analyse(obs(stub=("a", "f")))["flags"]["stub_md5_differs_from_received"] == [3]


@pytest.mark.parametrize("state", ["absent", "partial", "invalid", "weird"])
def test_a_missing_partial_or_invalid_stub_digest_is_incomplete_never_a_success(state):
    result = analyse(obs(state=state))
    assert result["conclusion"] == "incomplete" and result["rows"] == ["R5"] and not result["complete"]


def test_an_empty_or_malformed_md5_is_never_valid_and_a_short_plan_is_incomplete():
    rows = obs()
    rows[0]["md5"] = ""
    assert analyse(rows)["conclusion"] == "incomplete"
    rows = obs()
    rows[1]["stub"] = None
    assert analyse(rows)["conclusion"] == "incomplete"
    assert analyse(obs()[:3])["conclusion"] == "incomplete"
    assert analyse([])["conclusion"] == "incomplete"


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


def run_recipe(tmp_path, url, session=None, *flags, ephemeral_ok=True):
    document = {**valid(), "evidence_root": str(tmp_path / "evidence"), "operator_timeout_s": 40, "operation_timeout_s": 60,
                "consent": {"scenarios": ["P4-FLASH-INTEGRITY"]}, "integrity": {"expected_assets_sha256": ASSETS}, **(session or {})}
    (tmp_path / "session.json").write_text(json.dumps(document))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json"),
           "PYTHONPATH": str(BENCH / "python/src")}
    run = subprocess.run([sys.executable, "-m", "pytest", RECIPE, "--bench", "--qual-allow-dirty", *(["--qual-allow-ephemeral-evidence"] if ephemeral_ok else []), *flags, "--bench-results", str(tmp_path / "runs"),
                          "-q", "-rs", "-p", "no:cacheprovider", "--tb=short"], cwd=BENCH, env=env, capture_output=True, text=True, timeout=240)
    reports = {}
    for path in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(path.read_text())["tests"]:
            reports[item["id"]] = item
    return run, reports


def case_dir(tmp_path):
    return next((tmp_path / "evidence").glob("PC-7-*"))


def submitted(tmp_path):
    rows = [json.loads(line) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows if r["method"] == "POST" and r["path"] == "/api/v1/operations"]


def test_concordant_digests_pass_with_exactly_six_read_only_operations(sim, tmp_path):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-7"]["status"] == "PASS", run.stdout[-1800:] + run.stderr[-300:]
    cmds = [json.loads(line) for line in (case_dir(tmp_path) / "board-0" / "commands.jsonl").read_text().splitlines()]
    assert [c["command"] for c in cmds] == ["identify", "flash.md5", "flash.read", "flash.md5", "flash.read", "flash.md5"]
    assert all(c["args"].get("return_console") is True for c in cmds[1:])
    assert cmds[2]["args"]["stub_digest"] is True and "stub_digest" not in cmds[1]["args"]
    analysis = json.loads((case_dir(tmp_path) / "integrity-analysis.json").read_text())
    assert analysis["conclusion"] == "consistent"
    assert client.device("board")["reservation"] is None
    assert client.artifacts()["artifacts"] == [], "server readbacks deleted after their private copy was verified"
    copies = sorted((case_dir(tmp_path) / "board-0").glob("flash-*-00002000.bin"))
    assert len(copies) == 2 and all(stat.S_IMODE(c.stat().st_mode) == 0o600 for c in copies)
    for copy in copies:
        side = json.loads(copy.with_suffix(".json").read_text())
        assert side["sha256"] == hashlib.sha256(copy.read_bytes()).hexdigest() and side["size"] == 4096
    assert any("does not authorise the full P4 backup" in line for line in reports["PC-7"]["evidence"])
    assert any(line.startswith("evidence storage:") for line in reports["PC-7"]["evidence"])


def test_received_bytes_corrupted_with_stable_stub_digest_fails_with_the_transfer_hypothesis(sim, tmp_path):
    url, client = sim({"SIM_INTEGRITY": "transport"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-7"]["status"] == "FAIL", run.stdout[-1500:]
    analysis = json.loads((case_dir(tmp_path) / "integrity-analysis.json").read_text())
    assert "R1" in analysis["rows"] and analysis["conclusion"] == "discordance"
    assert len(list((case_dir(tmp_path) / "board-0").glob("flash-*.bin"))) == 2, "the received bytes are kept even when the verdict is FAIL"
    assert client.artifacts()["artifacts"] == []
    assert len(submitted(tmp_path)) == 6, "no retry, no extra operation"


def test_a_flash_that_reads_differently_varies_the_stub_digest_too(sim, tmp_path):
    url, _ = sim({"SIM_INTEGRITY": "flash"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-7"]["status"] == "FAIL"
    assert "R2" in json.loads((case_dir(tmp_path) / "integrity-analysis.json").read_text())["rows"]


def test_a_chip_md5_that_disagrees_with_the_reads_fails(sim, tmp_path):
    url, _ = sim({"SIM_INTEGRITY": "md5cmd"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-7"]["status"] == "FAIL"
    assert json.loads((case_dir(tmp_path) / "integrity-analysis.json").read_text())["flags"]["chip_md5_differs_from_received"]


@pytest.mark.parametrize("state", ["absent", "partial", "invalid"])
def test_a_missing_partial_or_invalid_final_digest_is_a_fail_labelled_incomplete(sim, tmp_path, state):
    url, _ = sim({"SIM_STUB_DIGEST_STATE": state})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-7"]["status"] == "FAIL"
    assert json.loads((case_dir(tmp_path) / "integrity-analysis.json").read_text())["conclusion"] == "incomplete"


def test_an_invalid_md5_response_stops_at_the_first_command_without_retry(sim, tmp_path):
    url, client = sim({"SIM_MD5_INVALID": "1"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-7"]["status"] == "FAIL"
    failure = json.loads(next(case_dir(tmp_path).rglob("0-chip_md5-failure.json")).read_text())
    assert failure["error"]["code"] == "md5_response_invalid"
    assert len(submitted(tmp_path)) == 2, "identify and the failed md5 only"
    assert client.device("board")["reservation"] is None


def test_a_non_persistent_evidence_root_blocks_before_any_command(sim, tmp_path):
    from qualification_campaign.evidence import check_persistent_root
    if check_persistent_root(str(tmp_path))[0]:
        pytest.skip("the temporary directory of this machine is persistent: the blocking path cannot be exercised here")
    url, _ = sim()
    run, reports = run_recipe(tmp_path, url, None, ephemeral_ok=False)
    assert "BLOCKED" in run.stdout and "ephemeral" in run.stdout and not submitted(tmp_path)


def test_durable_copy_never_overwrites_existing_evidence_and_is_private(tmp_path):
    import importlib
    module = importlib.import_module("qualification_campaign.test_pc_integrity")
    data = bytes(range(256)) * 16
    path = module.durable_copy(tmp_path, 1, 0x2000, data, {"id": "a" * 32}, "op1")
    assert path.read_bytes() == data and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.with_suffix(".json").read_text())["artifact"] == "a" * 32
    with pytest.raises(FileExistsError):
        module.durable_copy(tmp_path, 1, 0x2000, b"other", {"id": "b" * 32}, "op2")
    assert path.read_bytes() == data, "the first evidence is untouched"


def test_a_read_failure_stops_the_sequence(sim, tmp_path):
    url, _ = sim({"SIM_FAIL_READ_AT": str(0x2000)})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-7"]["status"] == "FAIL"
    assert len(submitted(tmp_path)) == 3, "identify, md5, the failed read: nothing after"


def test_without_consent_nothing_is_submitted(sim, tmp_path):
    url, _ = sim()
    run, reports = run_recipe(tmp_path, url, {"consent": {"scenarios": []}})
    assert "BLOCKED" in run.stdout and not list((tmp_path / "evidence").glob("*/requests.jsonl")) or not submitted(tmp_path)


def test_wrong_target_or_missing_assets_digest_blocks_before_any_command(sim, tmp_path):
    url, _ = sim()
    run, _ = run_recipe(tmp_path, url, {"chip": "esp32c6", "flash_size": 8 * MIB})
    assert "BLOCKED" in run.stdout and not submitted(tmp_path)
    run, _ = run_recipe(tmp_path, url, {"integrity": {}})
    assert "BLOCKED" in run.stdout and not submitted(tmp_path)


def test_another_agent_version_blocks_before_any_command(sim, tmp_path):
    url, _ = sim({"SIM_ASSETS_SHA256": "cd" * 32})
    run, reports = run_recipe(tmp_path, url)
    assert reports.get("PC-7", {}).get("status") == "BLOCKED", run.stdout[-1200:]
    assert not submitted(tmp_path)


# --- protocol validation (pure) ---------------------------------------------------------------------------------------------------
sys.path.insert(0, str(BENCH.parent / "bench"))
from protocol import CAPABILITIES, PRIVILEGED, PROGRAM, Fault, validate_command          # noqa: E402

DEVICE = {"capabilities": ["identify", "flash.read", "flash.md5"], "flash_size": 32 * MIB, "chip": "esp32p4"}


def test_flash_md5_is_a_known_unprivileged_read_only_command():
    assert "flash.md5" in CAPABILITIES and "flash.md5" not in PRIVILEGED and "flash.md5" not in PROGRAM
    validate_command("flash.md5", {"address": 0x2000, "size": 4096}, DEVICE, {})
    validate_command("flash.md5", {"address": 0x2000, "size": 4096, "return_console": False}, DEVICE, {})


@pytest.mark.parametrize("args", [{"address": 0x2000, "size": 4096, "stub_digest": True}, {"address": 0x2000, "size": 4096, "extra": 1}, {"address": 0x2000},
                                  {"size": 4096}, {"address": -1, "size": 4096}, {"address": 0x2000, "size": 0}, {"address": 32 * MIB - 1, "size": 2},
                                  {"address": 0x2000, "size": 4096, "return_console": "yes"}])
def test_flash_md5_refuses_unknown_fields_and_bad_bounds(args):
    with pytest.raises(Fault):
        validate_command("flash.md5", args, DEVICE, {})


def test_flash_md5_needs_the_capability():
    with pytest.raises(Fault):
        validate_command("flash.md5", {"address": 0x2000, "size": 4096}, {**DEVICE, "capabilities": ["identify", "flash.read"]}, {})


def test_stub_digest_is_a_strict_boolean_on_flash_read_only():
    validate_command("flash.read", {"address": 0x2000, "size": 4096, "stub_digest": True}, DEVICE, {})
    for bad in (1, "true", None):
        with pytest.raises(Fault):
            validate_command("flash.read", {"address": 0x2000, "size": 4096, "stub_digest": bad}, DEVICE, {})
    with pytest.raises(Fault):
        validate_command("identify", {"stub_digest": True}, DEVICE, {})


# --- bitdiff (offline) --------------------------------------------------------------------------------------------------------------
def test_bitdiff_reports_positions_masks_and_directions_without_content():
    sys.path.insert(0, str(BENCH / "tools"))
    import bitdiff
    a = bytes(range(256)) * 16
    b = bytearray(a); b[2] ^= 0x80; b[0x10] ^= 0x01; b[0x20] ^= 0xC0
    rows = bitdiff.diff(a, bytes(b))
    assert [r["offset"] for r in rows] == [2, 0x10, 0x20]
    assert [r["bits"] for r in rows] == [1, 1, 2]
    assert sum(r["to_zero"] + r["to_one"] for r in rows) == 4
    summary = bitdiff.summarise(rows)
    assert summary["bytes"] == 3 and summary["bits"] == 4 and summary["spacings"] == [14, 16]
    with pytest.raises(ValueError):
        bitdiff.diff(b"ab", b"abc")
    result = bitdiff.compare({"x": a, "y": bytes(b), "z": a})
    assert result["differ_in_any_pair"] == 3 and result["differ_in_every_pair"] == 0
    assert str(a[:8]) not in json.dumps(result), "no dump content in the output"

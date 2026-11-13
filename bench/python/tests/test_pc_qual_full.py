"""Software checks of P4-QUAL-FULL (PC-11). SIMULATION ONLY: a RAM simulator that declares an esp32p4 with 32 MiB. It proves the recipe's decisions and fault handling,
NOTHING about the real card, the CH343, the loader, the official stub or esptool-js."""
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
from qualification_campaign import qual_full as qf                                      # noqa: E402
from qualification_campaign.session import SessionError, validate_qual_full, validate_session   # noqa: E402

RECIPE = "recipes/qualification_campaign/test_pc_qual_full.py"
SCENARIO = "P4-QUAL-FULL"
MIB = 1024 * 1024
P4 = 32 * MIB
ASSETS = "ab" * 32
GRANT = [[0.5, 14400]]
FULL = {"SIM_FLASH_MIB": "32", "SIM_FLASH_PATTERN": "sector-id", "SIM_JEDEC": "1", "SIM_ASSETS_SHA256": ASSETS}
REGIONS = [{"address": a, "size": 4096} for _, a in qf.D_SECTORS]


def valid(**change):
    base = {"version": 1, "board_id": "board", "chip": "esp32p4", "flash_size": P4, "transport": "external_uart", "partition_table_offset": None,
            "evidence_root": "/private/evidence", "accept_unknown_transport": True}
    return {**base, **change}


# --- pure ----------------------------------------------------------------------------------------------------------------
def test_patterns_are_deterministic_complementary_and_addressed():
    a = qf.pattern_a(0x800000)
    assert len(a) == 4096 and a == qf.pattern_a(0x800000) and a != qf.pattern_a(0x1000000)
    assert qf.pattern_b(0x800000) == bytes(x ^ 0xFF for x in a) and qf.blank() == b"\xff" * 4096
    assert qf.plan_checks() is True


def test_the_wrap_alias_of_d2_is_the_boot_sector():
    assert [qf.alias(a) for _, a in qf.D_SECTORS] == [0x1800000, 0x0, 0xFFF000] and all(qf.alias(qf.alias(a)) == a for _, a in qf.D_SECTORS)


def test_write_sectors_are_the_three_expected_ones():
    assert qf.D_SECTORS == (("D1", 0x800000), ("D2", 0x1000000), ("D3", 0x1FFF000))
    assert qf.WRITE_STEPS == ("pattern-A", "blank", "pattern-B", "restore") and qf.UART_BAUD == 115200


def test_session_section_is_validated():
    ok = validate_qual_full({"expected_assets_sha256": ASSETS})
    assert (ok["segment_size"], ok["grant_seconds"], ok["revoke_wait_s"]) == (MIB, 14400, 600)
    for bad in ({"grant_seconds": 100}, {"grant_seconds": 20000}, {"segment_size": 100}, {"expected_assets_sha256": "A" * 64}, {"x": 1}, {"revoke_wait_s": -1}):
        with pytest.raises(SessionError):
            validate_qual_full(bad)
    assert validate_session(valid(qual_full={"expected_assets_sha256": ASSETS}))["qual_full"]["expected_assets_sha256"] == ASSETS


# --- simulated bench -----------------------------------------------------------------------------------------------------
@pytest.fixture
def sim():
    started = []

    def start(env=None, script=GRANT, script_start="reservation"):
        process = subprocess.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--chip", "esp32p4", "--script", json.dumps(list(script)), "--script-start", script_start],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, **FULL, **(env or {})})
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


def run_recipe(tmp_path, url, session=None, *flags, destructive=True):
    document = {**valid(), "evidence_root": str(tmp_path / "evidence"), "operator_timeout_s": 40, "operation_timeout_s": 60, "consent": {"scenarios": [SCENARIO]},
                "approved_regions": REGIONS, "qual_full": {"expected_assets_sha256": ASSETS, "segment_size": 4 * MIB, "revoke_wait_s": 3}, **(session or {})}
    (tmp_path / "session.json").write_text(json.dumps(document))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json"), "PYTHONPATH": str(BENCH / "python/src")}
    run = subprocess.run([sys.executable, "-m", "pytest", RECIPE, "--bench", *(["--bench-allow-destructive"] if destructive else []), "--qual-allow-dirty", "--qual-allow-ephemeral-evidence",
                          *flags, "--bench-results", str(tmp_path / "runs"), "-q", "-rs", "-p", "no:cacheprovider", "--tb=short"], cwd=BENCH, env=env, capture_output=True, text=True, timeout=900)
    reports = {}
    for path in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(path.read_text())["tests"]:
            reports[item["id"]] = item
    return run, reports


def case_dir(tmp_path):
    return next((tmp_path / "evidence").glob("PC-11-*"))


def commands(tmp_path):
    path = case_dir(tmp_path) / "board-0" / "commands.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def phases(tmp_path):
    return json.loads((case_dir(tmp_path) / "phase-verdicts.json").read_text())


def expected_flash(size):
    return b"".join(i.to_bytes(4, "little") * 1024 for i in range(size // 4096))


@pytest.mark.slow_integration
def test_a_complete_campaign_passes_and_leaves_the_card_unchanged(sim, tmp_path):
    url, client = sim(script=[[0.5, 14400], [25, 0]])                     # the simulated operator grants, then revokes when asked
    run, reports = run_recipe(tmp_path, url, {"qual_full": {"expected_assets_sha256": ASSETS, "segment_size": 4 * MIB, "revoke_wait_s": 90}})
    assert reports["PC-11"]["status"] == "PASS", run.stdout[-2500:] + run.stderr[-300:]
    directory = case_dir(tmp_path) / "board-0"
    assert phases(tmp_path)["phases"] == {"A": "PASS", "B": "PASS", "C": "PASS", "D": "PASS"}
    for letter in "AB":
        assert (directory / f"BACKUP-{letter}.bin").read_bytes() == expected_flash(P4)
        assert oct((directory / f"BACKUP-{letter}.bin").stat().st_mode & 0o777) == "0o600"
    cmds = commands(tmp_path)
    writes = [c for c in cmds if c["command"] == "flash.write"]
    assert len(writes) == 12 and all(c["args"]["uart_baud"] == 115200 and c["args"]["return_console"] is True for c in writes)
    assert sorted({c["args"]["images"][0]["address"] for c in writes}) == [a for _, a in qf.D_SECTORS]
    assert [c["args"]["images"][0]["address"] for c in writes][::4] == [0x800000, 0x1FFF000, 0x1000000], "order D1 -> D3 -> D2"
    assert all(c["args"]["uart_baud"] == 115200 for c in cmds if c["command"] == "flash.read")
    assert not [c for c in cmds if c["command"] in ("flash.erase", "reset", "boot.download")]
    assert client.device("board")["reservation"] is None and not client.device("board")["quarantined"]
    with client.reserve("board", ttl=60) as board:
        assert board.read_flash(address=0x800000, size=4096, return_console=False) == expected_flash(P4)[0x800000:0x801000]


@pytest.mark.slow_integration
def test_a_corrupted_readback_after_a_write_fails_and_nothing_is_retried(sim, tmp_path):
    url, client = sim(env={"SIM_WRITE_CORRUPT_AT": "2"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-11"]["status"] == "FAIL", run.stdout[-1500:]
    assert phases(tmp_path)["phases"]["D"] != "PASS"
    assert len([c for c in commands(tmp_path) if c["command"] == "flash.write"]) == 2, "no retry, no automatic restore"


@pytest.mark.slow_integration
def test_a_failed_write_stops_the_campaign(sim, tmp_path):
    url, client = sim(env={"SIM_WRITE_FAIL_AT": "1"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-11"]["status"] in ("FAIL", "INDETERMINATE"), run.stdout[-1500:]
    assert len([c for c in commands(tmp_path) if c["command"] == "flash.write"]) == 1


@pytest.mark.slow_integration
def test_two_different_backups_stop_the_campaign_before_any_write(sim, tmp_path):
    url, client = sim(env={"SIM_FLIP_READ_AT": "30"})                    # a read in phase B differs from its twin
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-11"]["status"] == "FAIL", run.stdout[-1500:]
    assert not [c for c in commands(tmp_path) if c["command"] == "flash.write"]


@pytest.mark.slow_integration
@pytest.mark.parametrize("session,flags,destructive", [
    ({"consent": {"scenarios": []}}, (), True),
    ({}, (), False),
    ({"approved_regions": REGIONS[:2]}, (), True),
    ({"approved_regions": REGIONS + [{"address": 0x2000, "size": 4096}]}, (), True),
    ({"qual_full": {"segment_size": 4 * MIB}}, (), True),
    ({"chip": "esp32s3", "flash_size": 8 * MIB}, (), True)], ids=["consent", "destructive", "fewer-regions", "extra-region", "assets-undeclared", "target"])
def test_blocked_before_any_command(sim, tmp_path, session, flags, destructive):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url, session, *flags, destructive=destructive)
    assert "PC-11" not in reports or reports["PC-11"]["status"] == "BLOCKED", run.stdout[-1200:]
    assert not list((tmp_path / "evidence").glob("*/board-*/commands.jsonl")) or not [c for c in commands(tmp_path) if c["command"].startswith("flash.")]
    assert client.artifacts()["artifacts"] == []


@pytest.mark.slow_integration
def test_without_a_local_grant_nothing_is_submitted(sim, tmp_path):
    url, client = sim(script=[])
    run, reports = run_recipe(tmp_path, url, {"operator_timeout_s": 5})
    assert reports["PC-11"]["status"] == "BLOCKED", run.stdout[-1500:]
    assert not [c for c in commands(tmp_path) if c["command"].startswith("flash.")]


@pytest.mark.slow_integration
def test_a_write_sector_identical_to_its_wrap_alias_is_blocked_before_any_write(sim, tmp_path):
    url, client = sim(env={"SIM_FLASH_PATTERN": "blank"})                # D2 and the boot sector hold the same bytes: a 16-MiB wrap cannot be excluded
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-11"]["status"] == "BLOCKED", run.stdout[-1500:]
    assert not [c for c in commands(tmp_path) if c["command"] == "flash.write"]


def test_write_order_and_block_guard_are_pure_properties():
    assert qf.WRITE_ORDER == ("D1", "D3", "D2") and qf.erase_block(0x1FFF000) == 0x1FF0000 and qf.erase_block(0x1000000) == 0x1000000
    table = [{"type": 0, "subtype": 0x10, "offset": 0x1FF0000, "size": 0x8000, "label": "app"}]
    assert qf.block_critical(0x1FFF000, table)[0] == 0x1FF0000 and qf.block_critical(0x1FFF000, []) is None
    assert qf.block_critical(0x1000000, table) is None and qf.block_critical(0x8000, [])[0] == 0x0       # the boot block is always critical


@pytest.mark.slow_integration
def test_a_d3_write_that_wraps_at_16_mib_is_caught_before_d2_and_sector_0_is_never_written(sim, tmp_path):
    url, client = sim(env={"SIM_WRAP_WRITES": "1"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-11"]["status"] == "FAIL", run.stdout[-1500:]
    writes = [c["args"]["images"][0]["address"] for c in commands(tmp_path) if c["command"] == "flash.write"]
    assert writes == [0x800000] * 4 + [0x1FFF000], "D1 completes, D3 stops at its first write, D2 is never engaged"
    assert 0x1000000 not in writes and 0x0 not in writes
    with client.reserve("board", ttl=60) as board:                               # the simulated wrap hit the alias 0xFFF000, never sector 0
        assert board.read_flash(address=0, size=4096, return_console=False) == expected_flash(P4)[:4096]

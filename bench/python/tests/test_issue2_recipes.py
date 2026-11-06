"""Software checks of S3R-3 (console return) and R4 (multi-image). SIMULATION ONLY: a RAM simulator that DECLARES itself an esp32s3.
Proves the decisions of the recipes (BLOCKED / PASS / FAIL) and that the control-window oracle sees a simulated implicit erase.
It proves nothing about a card, a UART bridge or a real flash."""
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_c3_read_only import table_image                       # noqa: E402

S3 = {"version": 1, "board_id": "board", "chip": "esp32s3", "flash_size": 4 * 1024 * 1024, "transport": "external_uart",
      "accept_unknown_transport": True, "partition_table_offset": 0x10000, "operator_timeout_s": 90}
RECIPES = "recipes/qualification_campaign"


@pytest.fixture
def sim():
    started = []

    def start(script=(), env=None):
        process = subprocess.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--script", json.dumps(list(script)), "--chip", "esp32s3"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, **(env or {})})
        started.append(process)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10)
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
        process.communicate(timeout=5)


def write_table(client, entries):
    image = table_image(entries)
    artifact = client.upload(image)
    with client.reserve("board", ttl=120, allow_destructive=True) as board:
        board.command("flash.write", {"chip": "esp32s3", "return_console": False,
                                      "images": [{"address": 0x10000, "artifact": artifact["id"], "sha256": artifact["sha256"]}]}, allow_destructive=True)
    client.delete_artifact(artifact["id"])


def wait_grant(client, positive, deadline):
    while (client.device("board")["privileged_seconds"] > 0) != positive:
        assert time.monotonic() < deadline
        time.sleep(0.05)


def run(tmp_path, url, selector, session, *args):
    (tmp_path / "session.json").write_text(json.dumps({**S3, "evidence_root": str(tmp_path / "evidence"), **session}))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json")}
    proc = subprocess.run([sys.executable, "-m", "pytest", f"{RECIPES}/{selector}", "--bench", "--qual-allow-dirty", "--bench-results", str(tmp_path / "runs"),
                           "-q", "-rs", "-p", "no:cacheprovider", "--tb=short", *args], cwd=BENCH, env=env, capture_output=True, text=True, timeout=240)
    reports = {}
    for path in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(path.read_text())["tests"]:
            reports[item["id"]] = item
    return proc, reports


# --- S3R-3 ------------------------------------------------------------------------------------------------------------
READ = {"consent": {"scenarios": ["S3R-READ"]}, "readonly": {"reads": [{"address": 0, "size": 4096}]}}


def test_console_return_passes_with_a_transport_event_after_the_settled_read(sim, tmp_path):
    url, client = sim(env={"SIM_CONSOLE_EVENT": "1"})
    proc, reports = run(tmp_path, url, "test_s3_uart_console_return.py", READ)
    assert proc.returncode == 0 and reports["S3R-3"]["status"] == "PASS", proc.stdout[-1500:]
    assert any("TRANSPORT proof only" in e for e in reports["S3R-3"]["evidence"])
    assert client.device("board")["reservation"] is None


def test_a_settled_read_without_a_console_event_is_a_fail_even_if_uart_text_arrives(sim, tmp_path):
    url, client = sim()                                           # no transport event emitted
    proc, reports = run(tmp_path, url, "test_s3_uart_console_return.py", READ)
    assert reports["S3R-3"]["status"] == "FAIL" and "console return is not shown" in " ".join(reports["S3R-3"]["incidents"])


@pytest.mark.parametrize("change,why", [
    ({"consent": {"scenarios": []}}, "S3R-READ consent"),
    ({"readonly": {}}, "no readonly.reads"),
    ({"chip": "esp32c3"}, "targets an esp32s3"),
    ({"transport": "usb_serial_jtag"}, "external_uart transport"),
])
def test_console_return_is_blocked_before_any_command(sim, tmp_path, change, why):
    url, _ = sim()
    proc, reports = run(tmp_path, url, "test_s3_uart_console_return.py", {**READ, **change})
    assert not reports and why in proc.stdout
    assert not list((tmp_path / "evidence").glob("*/requests.jsonl"))


# --- R4 ---------------------------------------------------------------------------------------------------------------
R4 = {"consent": {"scenarios": ["R4"]},
      "approved_regions": [{"address": 0x200000, "size": 8192}, {"address": 0x210000, "size": 8192}],
      "r4": {"control_windows": [{"address": 0x10000, "size": 4096}, {"address": 0x300000, "size": 4096}]}}
TABLE = [("factory", 0x20000, 0x100000)]
SCRIPT = [[0.1, 600], [4, 0], [6, 1800], [40, 0]]             # setup grant, setup revoke, R4 grant, R4 revoke


def prepared(sim, env=None):
    url, client = sim(SCRIPT, env=env)
    deadline = time.monotonic() + 20
    wait_grant(client, True, deadline)
    write_table(client, TABLE)
    wait_grant(client, False, deadline + 10)
    return url, client


def test_multi_image_passes_and_restores_every_region(sim, tmp_path):
    url, client = prepared(sim)
    proc, reports = run(tmp_path, url, "test_r4_multi_image.py", R4, "--bench-allow-destructive")
    assert proc.returncode == 0 and reports["R4"]["status"] == "PASS", proc.stdout[-2000:]
    evidence = " ".join(reports["R4"]["evidence"])
    assert "single multi-image write" in evidence and "byte-identical after the write" in evidence and "after the restoration" in evidence
    flash = client.device("board")
    assert flash["privileged_seconds"] == 0 and flash["reservation"] is None


def test_a_simulated_implicit_global_erase_is_seen_by_the_control_windows(sim, tmp_path):
    url, client = prepared(sim, env={"SIM_FAULT_GLOBAL_ERASE": "1"})
    proc, reports = run(tmp_path, url, "test_r4_multi_image.py", R4, "--bench-allow-destructive")
    assert reports["R4"]["status"] == "FAIL" and "control window changed after the write" in " ".join(reports["R4"]["incidents"]), proc.stdout[-1500:]


@pytest.mark.parametrize("change,flag,why", [
    ({}, False, "--bench-allow-destructive not given"),
    ({"consent": {"scenarios": []}}, True, "lacks the R4 consent"),
    ({"approved_regions": [{"address": 0x200000, "size": 8192}]}, True, "needs 2..4"),
    ({"approved_regions": [{"address": 0x200000, "size": 8192}, {"address": 0x201000, "size": 8192}]}, True, "overlap each other"),
    ({"r4": {}}, True, "no r4.control_windows"),
    ({"partition_table_offset": None}, True, "partition_table_offset unknown"),
])
def test_r4_is_blocked_before_any_mutation(sim, tmp_path, change, flag, why):
    url, client = sim()
    proc, reports = run(tmp_path, url, "test_r4_multi_image.py", {**R4, **change}, *(["--bench-allow-destructive"] if flag else []))
    assert not reports and why in proc.stdout, proc.stdout[-800:]


def test_all_ff_control_windows_cannot_detect_an_erase_so_r4_is_blocked(sim, tmp_path):
    url, client = prepared(sim)                                    # a valid table exists, but the only window is all 0xFF
    proc, reports = run(tmp_path, url, "test_r4_multi_image.py",
                        {**R4, "r4": {"control_windows": [{"address": 0x300000, "size": 4096}]}}, "--bench-allow-destructive")
    assert reports["R4"]["status"] == "BLOCKED" and "all 0xFF" in " ".join(reports["R4"]["incidents"]), proc.stdout[-1200:]
    ops = [json.loads(l)["command"] for path in (tmp_path / "evidence").glob("*/board-*/operations.jsonl") for l in path.read_text().splitlines()]
    assert "flash.write" not in ops


def test_the_session_validator_bounds_the_r4_section():
    sys.path.insert(0, str(BENCH / "recipes"))
    from qualification_campaign.session import SessionError, validate_session
    base = {**S3, "evidence_root": "/private/evidence", "approved_regions": R4["approved_regions"]}
    for bad in [{"x": 1}, {"control_windows": [{"address": 1, "size": 4096}]}, {"control_windows": [{"address": 0x200000, "size": 4096}]},
                {"control_windows": [{"address": 0, "size": 131072}]}, {"owned_partitions": [""]}, {"control_windows": [{"address": 0}]}]:
        with pytest.raises(SessionError):
            validate_session({**base, "r4": bad})
    assert validate_session({**base, "r4": {"control_windows": [{"address": 0x10000, "size": 4096}]}})

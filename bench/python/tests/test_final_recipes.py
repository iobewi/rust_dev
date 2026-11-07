"""Software checks of the issue #2 finalisation recipes (native USB, triggers, reservation expiry, faults on reads, LAN HTTP, capture, two boards).

SIMULATION ONLY: a RAM simulator that DECLARES a chip / a transport. It proves the decisions of the recipes (BLOCKED / PASS / FAIL /
INDETERMINATE) and that the evidence has the expected shape. It proves NOTHING about a card, a USB interface or a network."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_c3_read_only import base, sim, table_image                      # noqa: E402,F401  (sim is a fixture)

BENCH = Path(__file__).resolve().parents[2]
RECIPES = "recipes/qualification_campaign"


def session(**change):
    document = base(chip="esp32s3", transport="usb_serial_jtag", accept_unknown_transport=False, partition_table_offset=0x10000,
                    consent={"scenarios": ["NUSB-READ", "NUSB-RESET", "T7", "R5", "H10", "U8", "M2"]},
                    readonly={"table_candidates": [0x10000], "reads": [{"address": 0, "size": 4096}],
                              "reset": {"marker_timeout_s": 5, "reconnect_timeout_s": 8}})
    return {**document, **change}


def run_recipe(tmp_path, url, selector, *flags, env=None, **session_change):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "session.json").write_text(json.dumps({**session(), "evidence_root": str(tmp_path / "evidence"), **session_change}))
    full = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json"),
            **(env or {})}
    proc = subprocess.run([sys.executable, "-m", "pytest", f"{RECIPES}/{selector}", "--bench", "--qual-allow-dirty", "--bench-results",
                           str(tmp_path / "runs"), "-q", "-rs", "-p", "no:cacheprovider", "--tb=short", *flags],
                          cwd=BENCH, env=full, capture_output=True, text=True, timeout=240)
    reports = {}
    for path in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(path.read_text())["tests"]:
            reports[item["id"]] = item
    return proc, reports


def write_table(client, address, entries):
    image = table_image(entries)
    artifact = client.upload(image)
    with client.reserve("board", ttl=120, allow_destructive=True) as board:
        board.command("flash.write", {"chip": "esp32s3", "return_console": False,
                                      "images": [{"address": address, "artifact": artifact["id"], "sha256": artifact["sha256"]}]},
                      allow_destructive=True)
    client.delete_artifact(artifact["id"])


def prepared(sim, monkeypatch, env=None):
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    url, client = sim([[0.1, 600], [4, 0]], chip="esp32s3")
    deadline = time.monotonic() + 10
    while client.device("board")["privileged_seconds"] <= 0:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    write_table(client, 0x10000, [("factory", 0x20000, 0x100000)])
    while client.device("board")["privileged_seconds"] > 0:
        assert time.monotonic() < deadline + 15
        time.sleep(0.1)
    return url, client


# --- NUSB (requirement 2) ---------------------------------------------------------------------------------------------
def test_nusb_reads_and_identity_pass_on_a_simulated_native_card(sim, tmp_path, monkeypatch):
    url, client = prepared(sim, monkeypatch, {"SIM_NATIVE": "1"})
    proc, reports = run_recipe(tmp_path, url, "test_native_usb.py::test_nusb0_identity_and_refusals")
    assert reports["NUSB-0"]["status"] == "PASS", proc.stdout[-1500:]
    assert any("io0 refused 400 unsupported" in e for e in reports["NUSB-0"]["evidence"])
    proc, reports = run_recipe(tmp_path / "b", url, "test_native_usb.py::test_nusb1_reads_and_table")
    assert reports["NUSB-1"]["status"] == "PASS", proc.stdout[-1500:]
    assert client.device("board")["reservation"] is None


@pytest.mark.parametrize("env,why", [({}, "does not match the observed card"), ({"SIM_NATIVE": "1", "SIM_NATIVE_WITH_IO0": "1"}, None)])
def test_nusb_blocks_a_non_native_card_and_fails_a_native_card_that_offers_io0(sim, tmp_path, monkeypatch, env, why):
    url, _ = prepared(sim, monkeypatch, env)
    proc, reports = run_recipe(tmp_path, url, "test_native_usb.py::test_nusb0_identity_and_refusals")
    if why:                                    # no usb_serial_jtag inference: OTG / bridge / unknown are never a PASS, never a FAIL
        assert reports["NUSB-0"]["status"] == "BLOCKED" and why in " ".join(reports["NUSB-0"]["incidents"]), proc.stdout[-800:]
    else:
        assert reports["NUSB-0"]["status"] == "FAIL" and "EN/BOOT commands offered" in " ".join(reports["NUSB-0"]["incidents"])


@pytest.mark.parametrize("change", [{"transport": "external_uart"}, {"consent": {"scenarios": []}}])
def test_nusb_is_blocked_before_any_command_by_the_session(sim, tmp_path, monkeypatch, change):
    url, _ = prepared(sim, monkeypatch, {"SIM_NATIVE": "1"})
    proc, reports = run_recipe(tmp_path, url, "test_native_usb.py::test_nusb0_identity_and_refusals", **change)
    assert not reports and "BLOCKED" in proc.stdout, proc.stdout[-800:]
    assert not list((tmp_path / "evidence").glob("*/requests.jsonl"))


@pytest.mark.parametrize("reenumerate", [False, True])
def test_nusb_reset_passes_with_or_without_reenumeration_and_says_which(sim, tmp_path, monkeypatch, reenumerate):
    url, client = prepared(sim, monkeypatch, {"SIM_NATIVE": "1", **({"SIM_REENUMERATE": "1"} if reenumerate else {})})
    proc, reports = run_recipe(tmp_path, url, "test_native_usb.py::test_nusb2_reset_and_reenumeration", "--qual-allow-reset")
    assert reports["NUSB-2"]["status"] == "PASS", proc.stdout[-1800:] + " ".join(reports["NUSB-2"]["incidents"])
    seen = json.loads(next((tmp_path / "evidence").glob("NUSB-2-*/reenumeration.json")).read_text())
    assert seen["observed"] is reenumerate and (seen["generation_before"] != seen["generation_after"]) is reenumerate
    ops = {json.loads(l)["id"]: json.loads(l)["command"] for p in (tmp_path / "evidence").glob("*/board-*/operations.jsonl") for l in p.read_text().splitlines()}
    assert list(ops.values()) == ["reset"]                                       # exactly one reset, never replayed


def test_nusb_reset_needs_its_opt_in(sim, tmp_path, monkeypatch):
    url, _ = prepared(sim, monkeypatch, {"SIM_NATIVE": "1"})
    proc, reports = run_recipe(tmp_path, url, "test_native_usb.py::test_nusb2_reset_and_reenumeration")
    assert not reports and "--qual-allow-reset not given" in proc.stdout


# --- T7 (requirement 7) -----------------------------------------------------------------------------------------------
def test_trigger_fires_once_and_rearm_cancels_the_pending_action(sim, tmp_path, monkeypatch):
    url, client = sim([[6, 600], [50, 0]], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_triggers.py", transport="external_uart", accept_unknown_transport=True)
    assert reports["T7"]["status"] == "PASS", proc.stdout[-2000:] + " ".join(reports["T7"]["incidents"])
    seen = json.loads(next((tmp_path / "evidence").glob("T7-*/trigger-observations.json")).read_text())
    fire, cancel = seen["cases"]
    assert (len(fire["banners"]), len(fire["triggers"]), len(cancel["banners"]), len(cancel["triggers"])) == (2, 1, 1, 1)
    assert client.device("board")["reservation"] is None and client.device("board")["privileged_seconds"] == 0


def test_trigger_recipe_needs_its_consent(sim, tmp_path):
    url, _ = sim([], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_triggers.py", consent={"scenarios": []})
    assert not reports and "BLOCKED" in proc.stdout and not list((tmp_path / "evidence").glob("*/requests.jsonl"))


# --- R5 (requirements 5 and 6): interruption during a read ------------------------------------------------------------
FAULT = {"read": {"address": 0x100000, "size": 0x100000, "interrupt_after_s": 1}}


def test_interrupted_read_is_never_replayed_keeps_the_block_and_needs_the_operators_recovery(sim, tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_DROP_DURING_READ", "1")
    url, client = sim([[8, "recover"]], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_interruption.py::test_r5_reload", transport="external_uart", accept_unknown_transport=True, faults=FAULT)
    assert reports["R5-RELOAD"]["status"] == "PASS", proc.stdout[-2500:] + " ".join(reports["R5-RELOAD"]["incidents"])
    seen = json.loads(next((tmp_path / "evidence").glob("R5-RELOAD-*/interruption.json")).read_text())
    assert seen["state"] == "indeterminate" and seen["quarantined_after_reconnect"] is True and seen["submissions"] == 1
    assert any("BLOCKED" in e for e in reports["R5-RELOAD"]["evidence"]) and any("only after the operator" in e for e in reports["R5-RELOAD"]["evidence"])


def test_a_read_that_completes_before_the_fault_is_blocked_not_passed(sim, tmp_path):
    url, _ = sim([], chip="esp32s3")                                   # nothing interrupts the read
    proc, reports = run_recipe(tmp_path, url, "test_interruption.py::test_r5_reload", transport="external_uart", accept_unknown_transport=True, faults=FAULT)
    assert reports["R5-RELOAD"]["status"] == "BLOCKED" and "completed before the fault" in " ".join(reports["R5-RELOAD"]["incidents"]), proc.stdout[-1500:]


@pytest.mark.parametrize("selector,change", [("test_r5_reload", {"consent": {"scenarios": []}}), ("test_r5_unplug", {"faults": {}}),
                                             ("test_r5_restart", {})])
def test_r5_is_blocked_before_any_command(sim, tmp_path, selector, change):
    url, _ = sim([], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, f"test_interruption.py::{selector}", transport="external_uart", accept_unknown_transport=True,
                               **({"faults": FAULT} if "faults" not in change else {}), **change)
    assert not reports and "BLOCKED" in proc.stdout and not list((tmp_path / "evidence").glob("*/requests.jsonl")), proc.stdout[-800:]


# --- G9 (requirement 9): reservation expiry ---------------------------------------------------------------------------
def test_reservation_expires_by_itself_and_the_old_handle_is_refused(sim, tmp_path):
    url, client = sim([], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_reservation_expiry.py", transport="external_uart", accept_unknown_transport=True)
    assert reports["G9"]["status"] == "PASS", proc.stdout[-1800:] + " ".join(reports["G9"]["incidents"])
    refusals = [json.loads(l) for p in (tmp_path / "evidence").glob("G9-*/requests.jsonl") for l in p.read_text().splitlines()]
    codes = [r.get("code") for r in refusals if r.get("status")]
    assert codes.count("lease_expired") >= 2 and "device_reserved" in codes
    assert not client.device("board")["quarantined"]


# --- M2 (requirement 9): two cards of one family -----------------------------------------------------------------------
@pytest.fixture
def sim_two():
    import json as _json
    import selectors
    import subprocess as sp
    from iobewi_bench import BenchClient
    processes = []

    def start():
        process = sp.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--second", "--chip", "esp32s3"], stdout=sp.PIPE, stderr=sp.PIPE, text=True)
        processes.append(process)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10)
        url = _json.loads(process.stdout.readline())["url"]
        client = BenchClient(url, "pytest-client-test-token")
        deadline = time.monotonic() + 10
        while len(client.devices()) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        return url, client
    yield start
    for process in processes:
        process.terminate()
        process.communicate(timeout=5)


def test_two_boards_are_reserved_together_and_released_independently(sim_two, tmp_path):
    url, client = sim_two()
    proc, reports = run_recipe(tmp_path, url, "test_two_boards.py", transport="external_uart", accept_unknown_transport=True, second_board={"board_id": "board2"})
    assert reports["M2"]["status"] == "PASS", proc.stdout[-1800:] + " ".join(reports["M2"]["incidents"])
    assert client.device("board")["reservation"] is None and client.device("board2")["reservation"] is None


@pytest.mark.parametrize("change", [{}, {"consent": {"scenarios": []}}, {"second_board": {"board_id": "missing"}}])
def test_two_boards_is_blocked_without_a_second_real_card(sim, tmp_path, change):
    url, _ = sim([], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_two_boards.py", transport="external_uart", accept_unknown_transport=True, **change)
    status = {k: v["status"] for k, v in reports.items()}
    assert status in ({}, {"M2": "BLOCKED"}) and "BLOCKED" in proc.stdout, proc.stdout[-800:]


# --- H10 (requirement 10): LAN HTTP ------------------------------------------------------------------------------------
def lan_address():
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    import ipaddress
    return address if ipaddress.ip_address(address).is_private and not ipaddress.ip_address(address).is_loopback else None


@pytest.fixture
def lan_target():
    import socket
    address = lan_address()
    if address is None:
        pytest.skip("no private non-loopback address in this environment")
    holder = socket.socket(); holder.bind((address, 0)); port = holder.getsockname()[1]; holder.close()
    process = subprocess.Popen([sys.executable, str(BENCH / "tools/lan_http_target.py"), "--bind", address, "--port", str(port)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    origin = f"http://{address}:{port}"
    import urllib.request
    deadline = time.monotonic() + 10
    while True:
        try:
            urllib.request.urlopen(origin + "/__log", timeout=1).read()
            break
        except OSError:
            assert time.monotonic() < deadline
            time.sleep(0.1)
    yield origin
    process.terminate()
    process.communicate(timeout=5)


def test_lan_http_contract_passes_against_a_simulated_agent_and_a_real_target(sim, tmp_path, monkeypatch, lan_target):
    monkeypatch.setenv("SIM_HTTP_ORIGINS", lan_target)
    url, client = sim([[6, 600], [40, 0]], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_lan_http.py", transport="external_uart", accept_unknown_transport=True, lan_http={"origin": lan_target})
    assert reports["H10"]["status"] == "PASS", proc.stdout[-2000:] + " ".join(reports["H10"]["incidents"])
    seen = json.loads(next((tmp_path / "evidence").glob("H10-*/lan-observations.json")).read_text())
    assert [c["name"] for c in seen["cases"]] == ["ok", "redirect", "not_listed", "https", "credentials"]
    assert sorted(h["path"] for h in seen["target_hits"]) == ["/ok", "/redirect"]


@pytest.mark.parametrize("change,why", [({}, "no lan_http.origin"), ({"consent": {"scenarios": []}}, "H10 consent")])
def test_lan_http_is_blocked_without_a_target_or_consent(sim, tmp_path, change, why):
    url, _ = sim([], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_lan_http.py", transport="external_uart", accept_unknown_transport=True, **change)
    assert not reports and why in proc.stdout and not list((tmp_path / "evidence").glob("*/requests.jsonl"))


def test_lan_http_blocks_when_the_origin_is_not_listed_by_the_agent_or_the_target_is_unreachable(sim, tmp_path, lan_target):
    url, _ = sim([], chip="esp32s3")                         # the simulated agent lists no http_origins
    proc, reports = run_recipe(tmp_path, url, "test_lan_http.py", transport="external_uart", accept_unknown_transport=True, lan_http={"origin": lan_target})
    assert reports["H10"]["status"] == "BLOCKED" and "http_origins" in " ".join(reports["H10"]["incidents"])


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8099", "https://192.168.1.5:8099", "http://8.8.8.8:80", "http://192.168.1.5", "http://192.168.1.5:80/x",
                                    "http://user@192.168.1.5:80"])
def test_lan_http_session_refuses_loopback_public_https_or_ambiguous_origins(origin):
    sys.path.insert(0, str(BENCH / "recipes"))
    from qualification_campaign.session import SessionError, validate_session
    with pytest.raises(SessionError):
        validate_session({**session(), "evidence_root": "/private/e", "lan_http": {"origin": origin}})

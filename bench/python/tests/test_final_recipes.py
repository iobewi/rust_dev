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

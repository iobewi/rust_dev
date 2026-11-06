"""Software checks of the C3-R recipes. SIMULATION ONLY: a RAM simulator reports an esp32s3; no C3, no real card.

Verifies session validation, the decisions the recipes take (BLOCKED / PASS / INDETERMINATE), and that nothing is
presumed. It proves nothing about a C3, about native USB re-enumeration or about the physical interface."""
import json
import os
import selectors
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from iobewi_bench import BenchClient
from iobewi_bench.client import ContinuityError
from iobewi_bench.qualification import Campaign

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
from qualification_campaign import harness, readonly                       # noqa: E402
from qualification_campaign.harness import Outcome, recorded               # noqa: E402
from qualification_campaign.session import SessionError, build_manifest, table_candidates, validate_session   # noqa: E402

RECIPES = "recipes/qualification_campaign"


def base(**change):
    document = {"version": 1, "board_id": "board", "chip": "esp32s3", "flash_size": 4194304, "transport": "external_uart",
                "partition_table_offset": None, "evidence_root": "/private/evidence", "accept_unknown_transport": True,
                "consent": {"scenarios": ["C3R-READ", "C3R-RESET"]},
                "readonly": {"table_candidates": [0x8000, 0x10000], "reads": [{"address": 0, "size": 4096}],
                             "reset": {"marker_timeout_s": 5, "reconnect_timeout_s": 2}}}
    return {**document, **change}


# --- session ----------------------------------------------------------------------------------------------------
def test_unknown_offset_is_allowed_but_nothing_is_presumed():
    cfg = validate_session(base(readonly={}))
    assert cfg["partition_table_offset"] is None and table_candidates(cfg) == []
    assert cfg["readonly"]["reads"] == [] and cfg["readonly"]["table_candidates"] == []
    assert cfg["readonly"]["reset"]["marker"] == "ESP-ROM:esp32s3"          # derived from the declared chip, not hard-coded
    assert validate_session(base(partition_table_offset=0x8000, readonly={})) and table_candidates(
        validate_session(base(partition_table_offset=0x8000, readonly={}))) == [0x8000]


@pytest.mark.parametrize("readonly", [
    {"unknown": 1}, {"table_candidates": [0x8001]}, {"table_candidates": [0x8000, 0x8000]},
    {"table_candidates": [0x400000]}, {"table_candidates": [0, 4096, 8192, 12288, 16384]},
    {"reads": [{"address": 0, "size": 65537}]}, {"reads": [{"address": 0x3FF000, "size": 8192}]},
    {"reads": [{"address": 0}]}, {"reads": [{"address": 0, "size": 4096}] * 9},
    {"reset": {"marker": ""}}, {"reset": {"marker_timeout_s": 0}}, {"reset": {"x": 1}}])
def test_invalid_readonly_sections_are_refused(readonly):
    with pytest.raises(SessionError):
        validate_session(base(readonly=readonly))


@pytest.mark.parametrize("change", [{"consent": {"scenarios": ["C3R-NOPE"]}}, {"accept_unknown_transport": "yes"},
                                   {"partition_table_offset": 0x8001}])
def test_other_invalid_fields(change):
    with pytest.raises(SessionError):
        validate_session(base(**change))


def test_manifest_accepts_unknown_transport_only_when_declared_so():
    on, off = validate_session(base()), validate_session(base(accept_unknown_transport=False))
    assert build_manifest(on, "C3R", regions=[])["boards"][0]["transports"] == ["external_uart", "unknown"]
    assert build_manifest(off, "C3R", regions=[])["boards"][0]["transports"] == ["external_uart"]


# --- decoder and probes -----------------------------------------------------------------------------------------
def table_image(entries):
    raw = b"".join(b"\xaa\x50" + struct.pack("<BBII", 0, 16, start, size) + label.encode().ljust(16, b"\0") + struct.pack("<I", 0)
                   for label, start, size in entries)
    return raw.ljust(4096, b"\xff")


def test_independent_table_decoder_and_overlap_checks():
    image = table_image([("factory", 0x20000, 0x100000), ("nvs", 0x9000, 0x6000)])
    assert readonly.decode_table(image) == [("factory", 0x20000, 0x100000), ("nvs", 0x9000, 0x6000)]
    readonly.check_rows([{"address": 0x9000, "size": 0x6000}, {"address": 0x20000, "size": 0x1000}], 0x400000)
    with pytest.raises(AssertionError, match="overlap"):
        readonly.check_rows([{"address": 0x9000, "size": 0x6000}, {"address": 0xA000, "size": 0x1000}], 0x400000)
    with pytest.raises(AssertionError, match="beyond"):
        readonly.check_rows([{"address": 0x3FF000, "size": 0x2000}], 0x400000)


# --- simulated bench --------------------------------------------------------------------------------------------
@pytest.fixture
def sim():
    started = []

    def start(script=()):
        process = subprocess.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--script", json.dumps(list(script))],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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


def prepare(sim, tables):
    """Simulated card whose flash holds the given tables; the simulated operator grants, then revokes, before the run
    (a non-destructive campaign refuses to start while any grant is active)."""
    url, client = sim([[0.1, 600], [4, 0]])
    deadline = time.monotonic() + 10
    while client.device("board")["privileged_seconds"] <= 0:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    for address, entries in tables:
        write_table(client, address, entries)
    while client.device("board")["privileged_seconds"] > 0:
        assert time.monotonic() < deadline + 15
        time.sleep(0.1)
    return url, client


def write_table(client, address, entries):
    """Put a real partition table into the simulated flash through the public API."""
    image = table_image(entries)
    artifact = client.upload(image)
    with client.reserve("board", ttl=120, allow_destructive=True) as board:
        board.command("flash.write", {"chip": "esp32s3", "return_console": False,
                                      "images": [{"address": address, "artifact": artifact["id"], "sha256": artifact["sha256"]}]},
                      allow_destructive=True)


def run_recipe(tmp_path, url, selector, **session_change):
    session = {**base(), "evidence_root": str(tmp_path / "evidence"), **session_change}
    (tmp_path / "session.json").write_text(json.dumps(session))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json")}
    run = subprocess.run([sys.executable, "-m", "pytest", f"{RECIPES}/{selector}", "--bench", "--qual-allow-dirty",
                          "--qual-allow-reset", "--bench-results", str(tmp_path / "runs"), "-q", "-rs", "-p", "no:cacheprovider",
                          "--tb=short"], cwd=BENCH, env=env, capture_output=True, text=True, timeout=120)
    reports = {}
    for path in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(path.read_text())["tests"]:
            reports[item["id"]] = item
    return run, reports


def test_read_only_recipes_pass_against_a_simulated_card_with_one_valid_table(sim, tmp_path):
    url, client = prepare(sim, [(0x10000, [("factory", 0x20000, 0x100000)])])
    run, reports = run_recipe(tmp_path, url, "test_c3_read_only.py")
    assert run.returncode == 0 and "3 passed" in run.stdout, run.stdout[-1500:]
    assert {k: v["status"] for k, v in reports.items()} == {"C3R-0": "PASS", "C3R-1": "PASS", "C3R-2": "PASS"}
    assert any("transport declared=external_uart inferred=unknown" in e for e in reports["C3R-0"]["evidence"])   # both kept
    probes = json.loads(next((tmp_path / "evidence").glob("C3R-2-*/table-probes.json")).read_text())
    assert [(p["address"], p["result"]) for p in probes] == [(0x8000, "partition_table_invalid"), (0x10000, "valid")]
    assert client.device("board")["reservation"] is None


def test_no_valid_table_or_two_valid_tables_are_blocked_not_failed_or_passed(sim, tmp_path):
    url, client = prepare(sim, [])
    run, reports = run_recipe(tmp_path, url, "test_c3_read_only.py::test_c3r2_partition_table")
    assert reports["C3R-2"]["status"] == "BLOCKED" and "0 valid" in " ".join(reports["C3R-2"]["incidents"])
    url, client = prepare(sim, [(0x8000, [("a", 0x20000, 0x1000)]), (0x10000, [("b", 0x30000, 0x1000)])])
    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    run, reports = run_recipe(tmp2, url, "test_c3_read_only.py::test_c3r2_partition_table")
    assert reports["C3R-2"]["status"] == "BLOCKED" and "2 valid" in " ".join(reports["C3R-2"]["incidents"])


def test_a_wrong_declaration_is_blocked_before_any_hardware_command(sim, tmp_path):
    url, client = sim()
    for change, why in (({"chip": "esp32c3"}, "chip"), ({"accept_unknown_transport": False}, "transport")):
        tmp = tmp_path / why
        tmp.mkdir()
        run, reports = run_recipe(tmp, url, "test_c3_read_only.py::test_c3r0_identity", **change)
        assert reports["C3R-0"]["status"] == "BLOCKED", (why, run.stdout[-800:])
        requests = [json.loads(l) for l in next((tmp / "evidence").glob("C3R-0-*/requests.jsonl")).read_text().splitlines()]
        assert not [r for r in requests if r["method"] == "POST" and r["path"] == "/api/v1/operations"], "a command was sent"


def test_missing_consent_or_opt_in_skips_before_creating_any_evidence(sim, tmp_path):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url, "test_c3_read_only.py::test_c3r0_identity", consent={"scenarios": []})
    assert "1 skipped" in run.stdout and "C3R-READ" in run.stdout and not reports
    assert not list((tmp_path / "evidence").glob("*")) if (tmp_path / "evidence").exists() else True
    run, reports = run_recipe(tmp_path, url, "test_c3_reset.py", consent={"scenarios": ["C3R-READ"]})
    assert "1 skipped" in run.stdout and "C3R-RESET" in run.stdout and not reports


def test_reads_never_send_a_reset_or_a_write(sim, tmp_path):
    url, client = prepare(sim, [(0x10000, [("factory", 0x20000, 0x100000)])])
    tmp = tmp_path / "reads"
    tmp.mkdir()
    run, reports = run_recipe(tmp, url, "test_c3_read_only.py")
    commands = set()
    for path in (tmp / "evidence").glob("*/board-*/operations.jsonl"):
        commands |= {json.loads(l)["command"] for l in path.read_text().splitlines()}
    assert commands and commands <= {"identify", "flash.read", "partitions", "console"}, commands


# --- reset: criteria decided in advance -----------------------------------------------------------------------------
def test_reset_recipe_passes_when_the_rom_banner_follows_the_reset(sim, tmp_path):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url, "test_c3_reset.py")
    assert run.returncode == 0 and reports["C3R-3"]["status"] == "PASS", run.stdout[-1200:]
    assert any("observed after the reset, same proof window" in e for e in reports["C3R-3"]["evidence"])


def test_reset_without_banner_is_a_fail_not_a_pass(sim, tmp_path):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url, "test_c3_reset.py", readonly={"reset": {"marker": "NEVER-EMITTED", "marker_timeout_s": 2}})
    assert reports["C3R-3"]["status"] == "FAIL" and "no 'NEVER-EMITTED'" in " ".join(reports["C3R-3"]["incidents"])


def test_usb_reenumeration_is_indeterminate_with_the_reconnection_recorded(sim, tmp_path, monkeypatch):
    """A continuity break after the reset: the window is closed explicitly, the outcome is NOT a pass."""
    url, client = sim()
    cfg = validate_session(base(evidence_root=str(tmp_path)))
    case = Campaign(client, "board", tmp_path / "run", __file__, "C3R-3", manifest=build_manifest(cfg, "C3R", regions=[]))
    case.start()
    board = case.reserve(ttl=30)

    def broken(*args, **kwargs):
        raise ContinuityError("Device disconnected or generation changed; no automatic resume")
    monkeypatch.setattr(board, "wait_uart", broken)
    out = Outcome()
    with pytest.raises(ContinuityError):
        with recorded(case, "C3R-3", out, preconditions="sim", limits="sim"):
            readonly.reset_and_observe(case, board, client, cfg, out)
    status = json.loads((case.directory / "report.json").read_text())["tests"][0]["status"]
    assert status == "INDETERMINATE"
    assert any("continuity broke after the reset" in e for e in out.evidence)
    assert any("NOT seen reconnected" in e for e in out.evidence)                 # the simulated card never re-enumerates
    assert "interrupted" in (case.directory / "windows.jsonl").read_text()


def test_missing_reset_capability_is_blocked(sim, tmp_path, monkeypatch):
    url, client = sim()
    cfg = validate_session(base(evidence_root=str(tmp_path)))
    case = Campaign(client, "board", tmp_path / "run", __file__, "C3R-3", manifest=build_manifest(cfg, "C3R", regions=[]))
    case.start()
    board = case.reserve(ttl=30)
    real = client.device
    monkeypatch.setattr(client, "device", lambda *a: {**real(*a), "capabilities": ["identify"]})
    out = Outcome()
    with pytest.raises(pytest.skip.Exception, match="BLOCKED"):
        with recorded(case, "C3R-3", out, preconditions="sim", limits="sim"):
            readonly.reset_and_observe(case, board, client, cfg, out)


class FakeBoard:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def command(self, name, args):
        self.calls.append((name, args))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, str):
            from iobewi_bench.client import OperationError
            raise OperationError({"id": "op", "state": "failed", "error": {"code": outcome}})
        return {"partitions": outcome}


def test_only_partition_table_invalid_lets_the_probe_move_on():
    from iobewi_bench.client import OperationError
    board = FakeBoard(["partition_table_invalid", [{"label": "x", "address": 1, "size": 1}]])
    valid, probes = readonly.probe_candidates(board, [0x8000, 0x10000])
    assert [v[0] for v in valid] == [0x10000] and [p["result"] for p in probes] == ["partition_table_invalid", "valid"]
    assert all(args["return_console"] is False for _, args in board.calls)         # reads keep the loader open
    board = FakeBoard(["serial_disconnected", [{"label": "x", "address": 1, "size": 1}]])
    with pytest.raises(OperationError):                                               # a USB / link fault is NOT "no table here"
        readonly.probe_candidates(board, [0x8000, 0x10000])
    assert len(board.calls) == 1, "the probe went on after a link fault"

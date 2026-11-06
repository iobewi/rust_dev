"""Software checks of the C3-R recipes. SIMULATION ONLY: a RAM simulator that DECLARES itself an esp32c3 (--chip, S3 for the
negative cases); no real C3, no real card.

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
from iobewi_bench.client import BenchError, ContinuityError
from iobewi_bench.qualification import Campaign

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
from qualification_campaign import harness, readonly                       # noqa: E402
from qualification_campaign.harness import Outcome, recorded               # noqa: E402
from qualification_campaign.session import SessionError, build_manifest, table_candidates, validate_session   # noqa: E402

RECIPES = "recipes/qualification_campaign"


def base(**change):
    document = {"version": 1, "board_id": "board", "chip": "esp32c3", "flash_size": 4194304, "transport": "external_uart",
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
    assert cfg["readonly"]["reset"]["marker"] == "ESP-ROM:esp32c3"          # derived from the declared chip, not hard-coded
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

    def start(script=(), chip="esp32c3"):
        process = subprocess.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--script", json.dumps(list(script)),
                                    "--chip", chip],
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
        board.command("flash.write", {"chip": "esp32c3", "return_console": False,
                                      "images": [{"address": address, "artifact": artifact["id"], "sha256": artifact["sha256"]}]},
                      allow_destructive=True)
    client.delete_artifact(artifact["id"])                      # the test's own upload must not linger on the server


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


def commands_sent(directory):
    requests = [json.loads(l) for path in directory.glob("*/requests.jsonl") for l in path.read_text().splitlines()]
    return [r for r in requests if r["method"] == "POST" and r["path"] == "/api/v1/operations"]


def test_a_coherent_s3_session_cannot_pass_the_c3_recipes(sim, tmp_path):
    """A declared (and even observed) S3 must be BLOCKED before any evidence is created or any command is sent."""
    url, client = sim(chip="esp32s3")
    for selector in ("test_c3_read_only.py", "test_c3_reset.py"):
        tmp = tmp_path / selector[:-3]
        tmp.mkdir()
        run, reports = run_recipe(tmp, url, selector, chip="esp32s3")
        assert run.returncode == 0 and "passed" not in run.stdout and "BLOCKED" in run.stdout and "esp32c3" in run.stdout, run.stdout[-900:]
        assert not reports and not commands_sent(tmp / "evidence")
        assert not list((tmp / "evidence").glob("*")) if (tmp / "evidence").exists() else True


def test_a_wrong_declaration_is_blocked_before_any_hardware_command(sim, tmp_path):
    url, client = sim()
    for change, why in (({"accept_unknown_transport": False}, "transport"),):
        tmp = tmp_path / why
        tmp.mkdir()
        run, reports = run_recipe(tmp, url, "test_c3_read_only.py::test_c3r0_identity", **change)
        assert reports["C3R-0"]["status"] == "BLOCKED", (why, run.stdout[-800:])
        requests = [json.loads(l) for l in next((tmp / "evidence").glob("C3R-0-*/requests.jsonl")).read_text().splitlines()]
        assert not [r for r in requests if r["method"] == "POST" and r["path"] == "/api/v1/operations"], "a command was sent"


def test_a_declared_c3_on_an_observed_s3_is_blocked_by_the_card_check(sim, tmp_path):
    url, client = sim(chip="esp32s3")
    run, reports = run_recipe(tmp_path, url, "test_c3_read_only.py::test_c3r0_identity")      # session declares esp32c3
    assert reports["C3R-0"]["status"] == "BLOCKED" and "does not match the observed card" in " ".join(reports["C3R-0"]["incidents"])
    assert not commands_sent(tmp_path / "evidence")


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


class IdentifyBoard:
    def __init__(self, events):
        self._events, self.console_calls = events, 0

    def cursor(self):
        return object()

    def identify(self):
        return {"chip": "esp32c3", "flash_size": 4194304}

    def events(self, cursor):
        return self._events

    def console(self):
        self.console_calls += 1


def test_identify_proves_the_console_return_and_adds_no_extra_restart_when_it_is_there():
    board, out = IdentifyBoard([{"kind": "transport", "detail": "console opened; opening a port may reset the MCU"}]), Outcome()
    readonly.identify_and_confirm_console(board, out)
    assert board.console_calls == 0 and "returned by the operation itself" in out.evidence[0]
    assert "TRANSPORT evidence only" in out.evidence[0] and "application restarted correctly" in out.evidence[0]


def test_identify_issues_one_explicit_console_only_without_proof():
    board, out = IdentifyBoard([{"kind": "uart", "data": ""}]), Outcome()
    readonly.identify_and_confirm_console(board, out)
    assert board.console_calls == 1 and "explicit console() issued" in out.evidence[0]


def test_identify_recipe_leaves_the_simulated_card_in_the_console(sim, tmp_path):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url, "test_c3_read_only.py::test_c3r0_identity")
    assert reports["C3R-0"]["status"] == "PASS" and any("explicit console() issued" in e for e in reports["C3R-0"]["evidence"])
    commands = [json.loads(l)["command"] for p in (tmp_path / "evidence").glob("*/board-*/operations.jsonl") for l in p.read_text().splitlines()]
    assert [c for c in dict.fromkeys(commands)] == ["identify", "console"]            # nothing else, in this order


def test_reenumeration_with_a_new_generation_closes_and_reopens_the_window_but_stays_indeterminate(sim, tmp_path, monkeypatch):
    url, client = sim()
    cfg = validate_session(base(evidence_root=str(tmp_path)))
    case = Campaign(client, "board", tmp_path / "run", __file__, "C3R-3", manifest=build_manifest(cfg, "C3R", regions=[]))
    case.start()
    board = case.reserve(ttl=30)
    real = client.device
    state = {"broken": False}

    def broken(*args, **kwargs):
        state["broken"] = True                                   # from now on the card is a new session
        raise ContinuityError("USB device re-enumerated")
    monkeypatch.setattr(board, "wait_uart", broken)
    monkeypatch.setattr(client, "device", lambda *a: {**real(*a), "generation": "f" * 32} if state["broken"] else real(*a))
    out = Outcome()
    with pytest.raises(ContinuityError):
        with recorded(case, "C3R-3", out, preconditions="sim", limits="sim"):
            readonly.reset_and_observe(case, board, client, cfg, out)
    assert json.loads((case.directory / "report.json").read_text())["tests"][0]["status"] == "INDETERMINATE"
    assert any("card reconnected: generation" in e for e in out.evidence)
    events = [json.loads(l) for l in (case.directory / "windows.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["interrupted", "opened"]                  # old window closed, fresh one opened
    assert json.loads((case.directory / "cleanup.json").read_text())["errors"] == [], "the fresh window must close cleanly"


# --- reads leave the loader: the console is restored on every exit (review P1) -------------------------------------------
class ConsoleBoard:
    def __init__(self, *, closed=False, uncertain=False, pending=None, fail=None):
        self.closed, self.uncertain, self.pending, self.fail, self.console_calls = closed, uncertain, pending, fail, 0

    def console(self):
        self.console_calls += 1
        if self.fail:
            raise self.fail


def test_console_is_restored_once_on_the_normal_path_and_a_failure_there_is_not_swallowed():
    board, out = ConsoleBoard(), Outcome()
    with readonly.console_on_exit(board, out):
        pass
    assert board.console_calls == 1
    board = ConsoleBoard(fail=ContinuityError("gone"))
    with pytest.raises(ContinuityError):
        with readonly.console_on_exit(board, Outcome()):
            pass


@pytest.mark.parametrize("error", [harness.Blocked("two valid tables"), AssertionError("reads differ"), ValueError("x")])
def test_an_early_exit_restores_the_console_once_and_keeps_the_first_error(error):
    board, out = ConsoleBoard(), Outcome()
    with pytest.raises(type(error)) as raised:
        with readonly.console_on_exit(board, out):
            raise error
    assert raised.value is error and board.console_calls == 1
    assert any("console returned after an early exit" in e for e in out.evidence)


def test_an_unresolved_operation_means_no_console_attempt_and_an_explicit_recovery_note():
    for board in (ConsoleBoard(uncertain=True), ConsoleBoard(pending="op-1"), ConsoleBoard(closed=True)):
        out = Outcome()
        with pytest.raises(AssertionError):
            with readonly.console_on_exit(board, out):
                raise AssertionError("first error")
        assert board.console_calls == 0 and "local recovery required" in out.restoration
        assert any("NOT attempted" in i for i in out.incidents)


def test_a_console_that_cannot_be_restored_is_reported_without_masking_the_first_error():
    board, out = ConsoleBoard(fail=ContinuityError("USB gone")), Outcome()
    with pytest.raises(harness.Blocked):
        with readonly.console_on_exit(board, out):
            raise harness.Blocked("original")
    assert "left in the ROM loader" in out.restoration and any("console return failed" in i for i in out.incidents)


def test_blocked_table_probe_returns_the_simulated_card_to_the_console(sim, tmp_path):
    for name, tables in (("zero", []), ("two", [(0x8000, [("a", 0x20000, 0x1000)]), (0x10000, [("b", 0x30000, 0x1000)])])):
        url, client = prepare(sim, tables)
        tmp = tmp_path / name
        tmp.mkdir()
        run, reports = run_recipe(tmp, url, "test_c3_read_only.py::test_c3r2_partition_table")
        assert reports["C3R-2"]["status"] == "BLOCKED"
        commands = [json.loads(l)["command"] for p in (tmp / "evidence").glob("*/board-*/operations.jsonl") for l in p.read_text().splitlines()]
        assert list(dict.fromkeys(commands))[-1] == "console", (name, commands)           # the loader was left again
        assert any("console returned after an early exit" in e for e in reports["C3R-2"]["evidence"])


# --- an indeterminate reset is not a continuity error but must get the same recovery evidence (review P1) -----------------
@pytest.mark.parametrize("reconnect", [False, True])
def test_an_indeterminate_reset_is_observed_not_replayed_not_certified(sim, tmp_path, monkeypatch, reconnect):
    from iobewi_bench.client import PendingOperation
    url, client = sim()
    cfg = validate_session(base(evidence_root=str(tmp_path)))
    case = Campaign(client, "board", tmp_path / "run", __file__, "C3R-3", manifest=build_manifest(cfg, "C3R", regions=[]))
    case.start()
    board = case.reserve(ttl=30)
    real, state, calls = client.device, {"broken": False}, []

    def indeterminate(name, args=None, **kwargs):
        calls.append(name)
        state["broken"] = True                                  # the card disconnects between the target check and the status read
        raise PendingOperation("Operation x has an indeterminate hardware outcome")
    monkeypatch.setattr(board, "command", indeterminate)
    if reconnect:
        monkeypatch.setattr(client, "device", lambda *a: {**real(*a), "generation": "f" * 32} if state["broken"] else real(*a))
    out = Outcome()
    with pytest.raises(PendingOperation):
        with recorded(case, "C3R-3", out, preconditions="sim", limits="sim"):
            readonly.reset_and_observe(case, board, client, cfg, out)
    assert calls == ["reset"], "the reset was replayed"
    assert json.loads((case.directory / "report.json").read_text())["tests"][0]["status"] == "INDETERMINATE"
    assert any("UNKNOWN" in e and "not replayed, not certified" in e for e in out.evidence)
    events = [json.loads(l)["event"] for l in (case.directory / "windows.jsonl").read_text().splitlines()]
    assert events == (["interrupted", "opened"] if reconnect else ["interrupted"])
    assert any(("card reconnected" if reconnect else "NOT seen reconnected") in e for e in out.evidence)


# --- server-side readbacks are released only after the private copy is verified -------------------------------------------
class ReadBoard:
    """Mimics Board.read_flash: a private local copy + sidecar, and an artifact recorded in last_operation."""
    def __init__(self, directory, *, tamper=False, delete_error=None, uncertain=False):
        self.output_dir, self.tamper, self.uncertain, self.pending = directory, tamper, uncertain, None
        self.deleted, self.order, self.last_operation, self.n = [], [], None, 0
        self.client, self.closed, self.console_calls, self.console_fail, self.download_fail = self, False, 0, None, None

    def console(self):
        self.console_calls += 1
        if self.console_fail:
            raise self.console_fail

    def read_flash(self, *, address, size, timeout=600, return_console=True):
        assert return_console is False
        data = bytes([self.n % 250 + 1]) * size                    # never all zeros: a zeroed copy must differ
        self.n += 1
        artifact = {"id": f"artifact-{self.n:04d}-xxxx", "size": size, "sha256": harness.hashlib.sha256(data).hexdigest()}
        self.last_operation = {"id": f"op{self.n}", "result": {"artifact": artifact}}
        if self.download_fail:                                    # the operation settled, the download / disk write did not
            raise self.download_fail
        path = self.output_dir / f"flash-{self.n:03d}-{address:08x}.bin"
        path.write_bytes(b"\x00" * size if self.tamper else data)
        path.with_suffix(".json").write_text(json.dumps({"address": address, "size": size, "sha256": artifact["sha256"], "artifact": artifact["id"]}))
        return data

    def delete_artifact(self, artifact_id):
        self.order.append(("delete", (self.output_dir / next(p.name for p in self.output_dir.glob("flash-*.bin"))).exists()))
        if getattr(self, "fail", None):
            raise self.fail
        self.deleted.append(artifact_id)


def test_server_readbacks_are_deleted_only_after_the_local_copy_matched(tmp_path):
    board, out = ReadBoard(tmp_path), Outcome()
    with readonly.retained_reads(board, out) as reads:
        reads.read(address=0, size=4096)
        reads.read(address=0, size=4096)
        assert board.deleted == [], "nothing may be deleted while the test still runs"
    assert board.deleted == ["artifact-0001-xxxx", "artifact-0002-xxxx"] and all(exists for _, exists in board.order)
    assert sum("deleted after its private copy was re-read" in e for e in out.evidence) == 2


def test_a_missing_or_different_local_copy_keeps_the_server_artifact_and_fails_a_would_be_pass(tmp_path):
    board, out = ReadBoard(tmp_path, tamper=True), Outcome()
    with pytest.raises(AssertionError, match="cleanup failed"):
        with readonly.retained_reads(board, out) as reads:
            reads.read(address=0, size=4096)
    assert board.deleted == [] and any("KEPT" in i for i in out.incidents)
    missing, out = ReadBoard(tmp_path / "x"), Outcome()
    (tmp_path / "x").mkdir()
    with pytest.raises(AssertionError):
        with readonly.retained_reads(missing, out) as reads:
            reads.read(address=0, size=4096)
            next((tmp_path / "x").glob("flash-*.bin")).unlink()
    assert missing.deleted == []


def test_cleanup_never_masks_or_converts_an_earlier_failure_or_block(tmp_path):
    for error in (harness.Blocked("two valid tables"), AssertionError("reads differ"), ContinuityError("USB gone")):
        directory = tmp_path / type(error).__name__
        directory.mkdir()
        board, out = ReadBoard(directory), Outcome()
        board.fail = RuntimeError("server refused the deletion")
        with pytest.raises(type(error)) as raised:
            with readonly.retained_reads(board, out) as reads:
                reads.read(address=0, size=4096)
                raise error
        assert raised.value is error                                                        # the first error is what propagates
        assert any("server deletion failed" in i for i in out.incidents)                    # and the cleanup problem is recorded


def test_an_early_exit_still_releases_verified_readbacks(tmp_path):
    board, out = ReadBoard(tmp_path), Outcome()
    with pytest.raises(AssertionError):
        with readonly.retained_reads(board, out) as reads:
            reads.read(address=0, size=4096)
            raise AssertionError("diverging reads")
    assert board.deleted == ["artifact-0001-xxxx"]


def test_an_unresolved_operation_keeps_every_server_readback(tmp_path):
    board, out = ReadBoard(tmp_path), Outcome()
    with pytest.raises(readonly.PendingOperation):
        with readonly.retained_reads(board, out) as reads:
            reads.read(address=0, size=4096)
            board.uncertain = True
            raise readonly.PendingOperation("lost response")
    assert board.deleted == [] and any("unresolved" in i for i in out.incidents)


def test_c3_read_recipes_leave_no_artifact_on_the_simulated_server(sim, tmp_path):
    url, client = prepare(sim, [(0x10000, [("factory", 0x20000, 0x100000)])])
    run, reports = run_recipe(tmp_path, url, "test_c3_read_only.py")
    assert {k: v["status"] for k, v in reports.items()} == {"C3R-0": "PASS", "C3R-1": "PASS", "C3R-2": "PASS"}
    assert client.artifacts()["artifacts"] == [], "a readback stayed on the server"
    assert any("server readback" in e for e in reports["C3R-1"]["evidence"] + reports["C3R-2"]["evidence"])
    dumps = list((tmp_path / "evidence").glob("C3R-*/board-*/flash-*.bin"))
    assert len(dumps) == 3 and all(p.with_suffix(".json").exists() for p in dumps)          # the private copies are still there


def test_an_artifact_is_tracked_even_when_read_flash_fails_after_the_operation_settled(tmp_path):
    board, out = ReadBoard(tmp_path), Outcome()
    board.download_fail = BenchError("download interrupted")                            # raised after the artifact exists
    with pytest.raises(type(board.download_fail)) as raised:
        with readonly.retained_reads(board, out) as reads:
            reads.read(address=0, size=4096)
    assert str(raised.value) == "download interrupted"                                    # the read error is what propagates
    assert board.deleted == [], "no verified local copy: the server artifact is the only evidence"
    assert any("private local copy missing or different - server artifact KEPT" in i for i in out.incidents)


def run_exit_matrix(tmp_path, *, body_error=None, delete_error=None, console_error=None):
    board, out = ReadBoard(tmp_path), Outcome()
    board.fail, board.console_fail = delete_error, console_error
    raised = None
    try:
        with readonly.console_on_exit(board, out), readonly.retained_reads(board, out) as reads:
            reads.read(address=0, size=4096)
            if body_error:
                raise body_error
    except BaseException as error:        # noqa: BLE001
        raised = error
    return raised, board, out


def test_exit_priority_read_error_then_cleanup_error_then_console_error(tmp_path):
    body, deletion, console = AssertionError("diverging reads"), RuntimeError("server refused"), ContinuityError("USB gone")
    for name, kwargs, expected in (
            ("everything fails", dict(body_error=body, delete_error=deletion, console_error=console), body),
            ("cleanup + console fail on a good body", dict(delete_error=deletion, console_error=console), "cleanup failed"),
            ("console alone fails on a good body", dict(console_error=console), console),
            ("cleanup alone fails on a good body", dict(delete_error=deletion), "cleanup failed"),
            ("all good", dict(), None)):
        directory = tmp_path / name.replace(" ", "_")
        directory.mkdir()
        raised, board, out = run_exit_matrix(directory, **kwargs)
        if expected is None:
            assert raised is None and board.console_calls == 1 and board.deleted, name
        elif isinstance(expected, str):
            assert isinstance(raised, AssertionError) and expected in str(raised), (name, raised)
        else:
            assert raised is expected, (name, raised)
        if kwargs.get("delete_error"):
            assert any("server deletion failed" in i for i in out.incidents), name       # always recorded
        if kwargs.get("console_error"):
            assert "left in the ROM loader" in out.restoration or kwargs.get("body_error") is None, name
        if expected is not None:
            assert board.console_calls == 1, (name, "the console return must be attempted exactly once")

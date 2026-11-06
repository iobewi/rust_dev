"""Software checks of the versioned qualification recipes. SIMULATION ONLY: no board, no grant,
no real bench server. Hardware scenarios (Q1/Q2/Q3/C3) are NOT executed by this file."""
import json
import os
import selectors
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from iobewi_bench import BenchClient
from iobewi_bench.client import BenchError, ContinuityError, PendingOperation
from iobewi_bench.qualification import Campaign

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
from qualification_campaign import harness, restart             # noqa: E402
from qualification_campaign.harness import Blocked, Outcome, recorded   # noqa: E402
from qualification_campaign.q3_flow import coordinated_restart    # noqa: E402
from qualification_campaign.session import (SessionError, build_manifest, validate_session,  # noqa: E402
                                            validate_manifest_for)

TEMPLATE = BENCH / "recipes/qualification_campaign/session.example.json"


def valid(**change):
    base = {"version": 1, "board_id": "b", "chip": "esp32s3", "flash_size": 0x1000000, "transport": "external_uart",
            "partition_table_offset": 0x10000, "evidence_root": "/private/evidence"}
    return {**base, **change}


def test_template_is_not_usable_and_grants_nothing():
    document = json.loads(TEMPLATE.read_text())
    assert document["approved_regions"] == [] and document["consent"]["scenarios"] == []
    with pytest.raises(SessionError):
        validate_session(document)                              # placeholders cannot be run as-is


def test_no_destructive_region_by_default():
    cfg = validate_session(valid())
    assert cfg["approved_regions"] == [] and cfg["consent"]["scenarios"] == []
    assert build_manifest(cfg, "Q1")["write_regions"] == []


@pytest.mark.parametrize("change", [
    {"version": 2}, {"unknown": 1}, {"chip": "esp8266"}, {"transport": "bluetooth"},
    {"partition_table_offset": 0x10001}, {"evidence_root": "relative/path"},
    {"approved_regions": [{"address": 0xFFF000, "size": 0x2000}]},     # beyond flash
    {"approved_regions": [{"address": 0x400000}]}, {"operation_timeout_s": 601}])
def test_invalid_sessions_rejected_before_any_bench_access(change):
    with pytest.raises(SessionError):
        validate_session(valid(**change))


def test_region_alignment_is_enforced_by_the_manifest():
    cfg = validate_session(valid(approved_regions=[{"address": 0x400001, "size": 0x2000}]))
    with pytest.raises(ValueError):
        validate_manifest_for(cfg, "Q1")
    ok = validate_session(valid(approved_regions=[{"address": 0x400000, "size": 0x2000}]))
    assert validate_manifest_for(ok, "Q1").sha256


def test_empty_region_list_is_a_valid_manifest_for_q2_q3():
    validate_manifest_for(validate_session(valid()), "Q2", regions=[])


def test_collection_without_optin_touches_no_network_and_skips_everything(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.3)
    env = {**os.environ, "BENCH_URL": f"http://127.0.0.1:{listener.getsockname()[1]}"}
    env.pop("BENCH_CLIENT_TOKEN", None)
    run = subprocess.run([sys.executable, "-m", "pytest", "recipes/qualification_campaign", "-q", "-rs",
                          "-p", "no:cacheprovider"], cwd=BENCH, env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    import re
    assert re.search(r"\d+ skipped", run.stdout) and "passed" not in run.stdout and "failed" not in run.stdout
    with pytest.raises(socket.timeout):
        listener.accept()                                       # nothing connected at import/collection
    listener.close()


def test_optin_without_session_is_blocked_not_passed(tmp_path):
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "x" * 20, "BENCH_URL": "http://127.0.0.1:9"}
    env.pop("BENCH_QUAL_SESSION", None)
    run = subprocess.run([sys.executable, "-m", "pytest", "recipes/qualification_campaign", "--bench", "-q", "-rs",
                          "-p", "no:cacheprovider"], cwd=BENCH, env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0 and "BLOCKED" in run.stdout and "passed" not in run.stdout


@pytest.fixture
def isolated_launcher(tmp_path):
    """A REAL launcher and server on a free port with a throw-away state dir (not the bench's)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"t" * 24: {"id": "claude", "role": "client", "scopes": ["read"]}}))
    log = tmp_path / "launcher.log"
    env = {**os.environ, "BENCH_PORT": str(port), "BENCH_STATE_DIR": str(state), "BENCH_TOKENS_FILE": str(tokens)}
    process = subprocess.Popen([sys.executable, str(BENCH / "tools/run_server.py"), "--shutdown-timeout", "10"],
                               env=env, stdout=subprocess.DEVNULL, stderr=log.open("wb"))
    (tmp_path / "launcher.pid").write_text(str(process.pid))
    client = BenchClient(f"http://127.0.0.1:{port}", "t" * 24)
    deadline = time.monotonic() + 30
    while True:
        try:
            client.health()
            break
        except Exception:
            assert time.monotonic() < deadline, "isolated server did not start"
            time.sleep(0.2)
    try:
        yield SimpleNamespace(port=port, url=f"http://127.0.0.1:{port}", state=state, pid_file=tmp_path / "launcher.pid",
                              client=client, process=process, log=log, root=tmp_path)
    finally:
        process.terminate()
        process.wait(timeout=20)


def _health(client):
    try:
        return client.health()
    except Exception:
        return None


def test_launcher_identification_restart_order_and_no_overlap(isolated_launcher):
    L = isolated_launcher
    before = L.client.health()
    launcher = restart.identify_launcher(L.pid_file, L.url, L.state)
    assert launcher.pid == L.process.pid
    fingerprint = restart.storage_fingerprint(L.state)
    offset = L.log.stat().st_size
    watch = restart.RestartWatch(launcher).start()
    restart.request_restart(launcher)
    assert restart.wait_for(lambda: restart.child_gone(launcher), 25), "old child must exit first"
    assert restart.wait_for(lambda: restart.replacement(launcher), 25)
    summary = watch.stop()
    after = restart.wait_for(lambda: _health(L.client), 25)
    assert after["epoch"] != before["epoch"] and after["durability"] == "sqlite-full"
    later = restart.storage_fingerprint(L.state)
    assert (later["db_inode"], later["lock_inode"]) == (fingerprint["db_inode"], fingerprint["lock_inode"])
    restart.verify_order(L.log.read_bytes()[offset:].decode(), launcher.child)     # exited before started
    assert summary["samples"] > 5 and not summary["overlap_seen"]


def _assert_untouched(L, launcher_child_ticks, epoch):
    assert restart.start_ticks(L.process.pid) is not None
    assert [k for k in restart.children(L.process.pid) if restart.is_server(k)], "server child vanished"
    assert L.client.health()["epoch"] == epoch, "A signal was sent although the server was not established"


@pytest.mark.parametrize("url", ["http://203.0.113.9:{port}", "http://bench.example.org:{port}", "ftp://127.0.0.1:{port}"])
def test_foreign_or_unsupported_endpoint_is_refused_without_signal(isolated_launcher, url):
    L = isolated_launcher
    epoch = L.client.health()["epoch"]
    with pytest.raises(restart.UnsupportedEndpoint):
        restart.identify_launcher(L.pid_file, url.format(port=L.port), L.state)
    _assert_untouched(L, None, epoch)


def test_endpoint_not_owned_by_the_child_is_refused_without_signal(isolated_launcher):
    L = isolated_launcher
    epoch = L.client.health()["epoch"]
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    try:
        with pytest.raises(restart.LauncherError, match="listening socket"):
            restart.identify_launcher(L.pid_file, f"http://127.0.0.1:{other.getsockname()[1]}", L.state)
    finally:
        other.close()
    _assert_untouched(L, None, epoch)


def test_other_state_directory_is_refused_even_with_stable_files(isolated_launcher):
    L = isolated_launcher
    epoch = L.client.health()["epoch"]
    decoy = L.root / "decoy-state"
    decoy.mkdir()
    (decoy / "operations.sqlite3").write_bytes(b"x")
    (decoy / "server.lock").write_bytes(b"")        # stable inodes that prove nothing about the running server
    with pytest.raises(restart.LauncherError, match="state_dir"):
        restart.identify_launcher(L.pid_file, L.url, decoy)
    _assert_untouched(L, None, epoch)


def test_stale_or_foreign_pid_files_are_refused(tmp_path):
    bogus = tmp_path / "launcher.pid"
    bogus.write_text(str(os.getpid()))                          # a live process that is NOT run_server.py
    with pytest.raises(restart.LauncherError):
        restart.identify_launcher(bogus, "http://127.0.0.1:8080", tmp_path)
    bogus.write_text("999999999")
    with pytest.raises(restart.LauncherError):
        restart.identify_launcher(bogus, "http://127.0.0.1:8080", tmp_path)


GOOD_LOG = "Bench server child started (PID 10).\nWaiting for server PID 10 to exit\nServer PID 10 exited (0).\nBench server child started (PID 11).\n"


def test_order_verification_accepts_exit_then_start_and_rejects_the_rest():
    assert restart.verify_order(GOOD_LOG, 10)["old_exit_line"] < restart.verify_order(GOOD_LOG, 10)["new_start_line"]
    premature = "Bench server child started (PID 11).\nServer PID 10 exited (0).\n"
    for bad in (premature, "Server PID 10 exited (0).\n", "Bench server child started (PID 11).\n",
                "Server PID 10 has not exited; replacement refused.\n"):
        with pytest.raises(restart.OrderError):
            restart.verify_order(bad, 10)


def test_watch_flags_a_defective_launcher_that_starts_the_replacement_early(tmp_path):
    server = tmp_path / "bench_server.py"
    server.write_text("import time\ntime.sleep(60)\n")
    launcher_script = tmp_path / "run_server.py"
    launcher_script.write_text(textwrap.dedent(f"""
        import signal, subprocess, sys, time
        spawn = lambda: subprocess.Popen([sys.executable, {str(server)!r}])
        old = spawn()
        def hup(*_):
            spawn()                       # DEFECT: replacement before the old child is gone
            time.sleep(0.4)
            old.terminate()
        signal.signal(signal.SIGHUP, hup)
        while True:
            time.sleep(0.05)
        """))
    process = subprocess.Popen([sys.executable, str(launcher_script)])
    try:
        assert restart.wait_for(lambda: [k for k in restart.children(process.pid) if restart.is_server(k)], 10)
        child = [k for k in restart.children(process.pid) if restart.is_server(k)][0]
        launcher = restart.Launcher(process.pid, restart.start_ticks(process.pid), child, restart.start_ticks(child), str(tmp_path), 0)
        watch = restart.RestartWatch(launcher).start()
        restart.request_restart(launcher)
        time.sleep(1.2)
        assert watch.stop()["overlap_seen"] is True
    finally:
        for kid in restart.children(process.pid):
            os.kill(kid, 15)
        process.terminate()
        process.wait(timeout=10)


# --- simulated bench (function scoped: reservations never leak between tests) -----------------------
@pytest.fixture
def sim():
    started = []

    def start(script=()):
        process = subprocess.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--script", json.dumps(list(script))],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        process.communicate(timeout=5)


def _verdict(case):
    return json.loads((case.directory / "report.json").read_text())["tests"][-1]["status"]


def test_lost_submission_response_stays_indeterminate(sim, tmp_path):
    url, client = sim()
    case = Campaign(client, "board", tmp_path / "run", __file__, "T")
    case.start()
    board = case.reserve(ttl=3)
    original = case.client._request

    def lossy(method, path, *args, **kwargs):
        result = original(method, path, *args, **kwargs)            # the server DID receive the submission
        if method == "POST" and path == "/api/v1/operations":
            raise BenchError("response lost")
        return result
    case.client._request = lossy
    out = Outcome()
    with pytest.raises(PendingOperation) as failure:
        with recorded(case, "T", out, preconditions="sim", limits="sim"):
            board.identify()
    assert isinstance(failure.value.__cause__, BenchError)          # first error preserved
    assert board.uncertain and _verdict(case) == "INDETERMINATE"
    assert "UNKNOWN" in out.restoration and not any("FAIL" in i for i in out.incidents)


def test_uncertainty_only_visible_at_close_is_not_a_pass_or_a_fail(sim, tmp_path):
    url, client = sim()
    case = Campaign(client, "board", tmp_path / "run", __file__, "T")
    case.start()
    board = case.reserve(ttl=3)
    out = Outcome()
    with pytest.raises(BaseException) as raised:
        with recorded(case, "T", out, preconditions="sim", limits="sim"):
            board.uncertain = True                                  # body "succeeds", outcome unknown
    assert any(isinstance(e, PendingOperation) for e in harness.flatten(raised.value))   # close error preserved
    assert _verdict(case) == "INDETERMINATE"


def test_provenance_identifies_reviewed_code_and_blocks_dirty_trees(sim, tmp_path, monkeypatch):
    url, client = sim()
    cfg = {"board_id": "board", "evidence_root": str(tmp_path)}
    config = SimpleNamespace(getoption=lambda name, default=None: {"--qual-allow-dirty": True, "--qual-session": None}.get(name, default))
    case = harness.make_campaign(client, cfg, "T", __file__, config, manifest=None)
    provenance = json.loads((case.directory / "provenance.json").read_text())
    names = set(provenance["support_sha256"])
    assert {"session.py", "harness.py", "restart.py", "q3_flow.py", "conftest.py"} <= names
    for name, digest in provenance["support_sha256"].items():          # executed copies, byte-identical
        import hashlib
        assert hashlib.sha256((case.directory / "support" / name).read_bytes()).hexdigest() == digest
    run = json.loads((case.directory / "run.json").read_text())
    assert run["declared_environment"]["recipe_git_sha"] == provenance["git_sha"] and len(provenance["git_sha"]) == 40
    monkeypatch.setattr(harness, "source_state", lambda: {"git_sha": "x" * 40, "clean": False, "dirty_paths": ["bench/x"]})
    strict = SimpleNamespace(getoption=lambda name, default=None: {"--qual-allow-dirty": False}.get(name, default))
    with pytest.raises(pytest.skip.Exception, match="clean committed tree"):
        harness.make_campaign(client, cfg, "T2", __file__, strict, manifest=None)


# --- Q2 operator flow against a simulated, scripted operator (NOT hardware) -------------------------
Q2_FLOWS = [("Q2a", "test_q2a_natural_expiry", [[1, 15]], {}),
            ("Q2b", "test_q2b_local_revocation", [[1, 400], [8, 0]], {}),
            ("Q2c", "test_q2c_new_reservation_cannot_disarm", [[1, 200], [12, 0]], {}),
            ("Q2d", "test_q2d_other_identity_cannot_disarm", [[1, 200], [12, 0]],
             {"BENCH_CLIENT_TOKEN_B": "pytest-second-test-token"})]


@pytest.mark.parametrize("scenario,test,script,extra", Q2_FLOWS)
def test_q2_flow_with_scripted_operator_and_simulated_grant_clock(sim, tmp_path, scenario, test, script, extra):
    url, client = sim(script)
    session = {"version": 1, "board_id": "board", "chip": "esp32s3", "flash_size": 4194304, "transport": "unknown",
               "partition_table_offset": 0x8000, "evidence_root": str(tmp_path / "evidence"), "operator_timeout_s": 40}
    (tmp_path / "session.json").write_text(json.dumps(session))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url,
           "BENCH_QUAL_SESSION": str(tmp_path / "session.json"), **extra}
    run = subprocess.run([sys.executable, "-m", "pytest", f"recipes/qualification_campaign/test_q2_trigger_disarm.py::{test}",
                          "--bench", "--qual-allow-dirty", "--bench-results", str(tmp_path / "runs"), "-q", "-rs",
                          "-p", "no:cacheprovider", "--tb=short"], cwd=BENCH, env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0 and "1 passed" in run.stdout, run.stdout[-1500:] + run.stderr[-500:]
    report = next((tmp_path / "evidence").glob(f"{scenario}-*/report.json"))
    assert json.loads(report.read_text())["tests"][0]["status"] == "PASS"
    assert client.device("board")["reservation"] is None


# --- Q3 flow ordering of effects (fake launcher operations, simulated bench) ------------------------
class FakeWatch:
    def __init__(self, launcher):
        pass

    def start(self):
        return self

    def stop(self):
        return {"samples": 1, "overlap_seen": False, "interval_s": 0.01}


def fake_ops(tmp_path, *, request, log):
    launcher = restart.Launcher(1, 1, 2, 2, str(tmp_path), 0)
    return SimpleNamespace(
        LauncherError=restart.LauncherError, RestartWatch=FakeWatch, verify_order=restart.verify_order,
        identify_launcher=lambda *a: launcher, storage_fingerprint=lambda d: {"db_inode": 1, "db_size": 1, "lock_inode": 2},
        request_restart=request, child_gone=lambda l: True, replacement=lambda l: 4242, wait_for=lambda f, t, i=0.5: f(),
        log=log)


def _q3_case(sim, tmp_path, monkeypatch_log=True):
    url, client = sim()
    log = tmp_path / "launcher.log"
    log.write_text("Bench server child started (PID 2).\n")
    cfg = {"board_id": "board", "q3": {**{"stop_timeout_s": 1, "health_timeout_s": 1, "agent_reconnect_timeout_s": 1},
           "launcher_pid_file": "x", "state_dir": str(tmp_path), "launcher_log": str(log)}}
    case = Campaign(client, "board", tmp_path / "run", __file__, "Q3")
    case.start()
    return url, client, cfg, case, log


def test_q3_failure_before_the_signal_releases_the_acquired_reservation(sim, tmp_path):
    url, client, cfg, case, log = _q3_case(sim, tmp_path)
    signalled = []

    def refuse(launcher):
        raise restart.LauncherError("Launcher identity changed since identification; no signal sent")
    out = Outcome()
    with pytest.raises(restart.LauncherError):
        with recorded(case, "Q3", out, preconditions="sim", limits="sim"):
            coordinated_restart(case, client, cfg, url, out, ops=fake_ops(tmp_path, request=refuse, log=log), reconnect_gesture=False, durability="volatile")
    assert client.device("board")["reservation"] is None, "reservation acquired before the signal was leaked"
    assert not signalled and _verdict(case) == "FAIL"


def test_q3_server_not_established_means_no_reservation_and_no_signal(sim, tmp_path):
    url, client, cfg, case, log = _q3_case(sim, tmp_path)
    ops = fake_ops(tmp_path, request=lambda l: pytest.fail("signal sent"), log=log)

    def foreign(*args):
        raise restart.UnsupportedEndpoint("remote URL")
    ops.identify_launcher = foreign
    out = Outcome()
    with pytest.raises(pytest.skip.Exception, match="BLOCKED"):
        with recorded(case, "Q3", out, preconditions="sim", limits="sim"):
            coordinated_restart(case, client, cfg, url, out, ops=ops, reconnect_gesture=False, durability="volatile")
    assert client.device("board")["reservation"] is None and not case.boards
    assert _verdict(case) == "BLOCKED"


def test_q3_failure_after_the_signal_abandons_the_stale_handle_without_touching_it(sim, tmp_path):
    url, client, cfg, case, log = _q3_case(sim, tmp_path)

    def hup(launcher):                               # the "launcher" logs exit then start, as the real one does
        with log.open("a") as stream:
            stream.write("Server PID 2 exited (0).\nBench server child started (PID 4242).\n")
    out = Outcome()
    board_before = None
    with pytest.raises(AssertionError, match="Epoch must change"):      # the simulated server never restarts
        with recorded(case, "Q3", out, preconditions="sim", limits="sim"):
            coordinated_restart(case, client, cfg, url, out, ops=fake_ops(tmp_path, request=hup, log=log), reconnect_gesture=False, durability="volatile")
    assert not case.boards                           # abandoned explicitly, never resumed
    assert (case.directory / "abandoned-handles.jsonl").exists()
    requests = [json.loads(line) for line in (case.directory / "requests.jsonl").read_text().splitlines()]
    assert not [r for r in requests if r.get("method") == "DELETE" and "/leases/" in str(r.get("path", r))], \
        "close() must not try to release the stale handle"
    assert client.device("board")["reservation"] is not None      # simulated server never restarted: lease still there
    assert _verdict(case) == "FAIL"


# --- continuity loss is INDETERMINATE (body, nested groups, close) -------------------------------------
def _recorded_case(sim, tmp_path):
    url, client = sim()
    case = Campaign(client, "board", tmp_path / "run", __file__, "T")
    case.start()
    case.reserve(ttl=3)
    return case, Outcome()


@pytest.mark.parametrize("make", [
    lambda: ContinuityError("Bench epoch changed"),
    lambda: BaseExceptionGroup("window", [ValueError("x"), ExceptionGroup("inner", [ContinuityError("generation changed")])])])
def test_continuity_loss_in_the_body_is_indeterminate_and_the_first_error_is_kept(sim, tmp_path, make):
    case, out = _recorded_case(sim, tmp_path)
    error = make()
    with pytest.raises(type(error)) as raised:
        with recorded(case, "T", out, preconditions="sim", limits="sim"):
            raise error
    assert raised.value is error                                       # not replaced, not replayed
    assert _verdict(case) == "INDETERMINATE" and any("continuity" in i for i in out.incidents)


def test_continuity_loss_reported_by_close_is_indeterminate(sim, tmp_path):
    case, out = _recorded_case(sim, tmp_path)

    def failing_close():
        raise BaseExceptionGroup("Qualification cleanup/evidence failed", [ExceptionGroup("events", [ContinuityError("epoch")])])
    case.close = failing_close
    with pytest.raises(BaseException) as raised:
        with recorded(case, "T", out, preconditions="sim", limits="sim"):
            pass                                                      # body succeeds
    assert any(isinstance(e, ContinuityError) for e in harness.flatten(raised.value))
    assert _verdict(case) == "INDETERMINATE"


def test_a_plain_failure_is_still_a_fail(sim, tmp_path):
    case, out = _recorded_case(sim, tmp_path)
    with pytest.raises(AssertionError):
        with recorded(case, "T", out, preconditions="sim", limits="sim"):
            assert False, "ordinary assertion"
    assert _verdict(case) == "FAIL"


# --- Q3 nominal path across the epoch change, with the REAL Board -----------------------------------------
def test_q3_flow_crosses_the_epoch_change_and_proves_the_old_real_board_invalid(sim, tmp_path):
    url, client, cfg, case, log = _q3_case(sim, tmp_path)
    cfg["q3"]["agent_reconnect_timeout_s"] = 5
    state = {"restarted": False}
    original = case.client._request

    def seen(method, path, *args, **kwargs):                        # the server "restarted": new epoch everywhere
        result = original(method, path, *args, **kwargs)
        if state["restarted"] and isinstance(result, dict) and "epoch" in result:
            result = {**result, "epoch": "restarted-epoch"}
        return result
    case.client._request = seen
    released_out_of_band = []

    def hup(launcher):
        lease = case.boards[0].lease_id
        client._request("DELETE", f"/api/v1/leases/{lease}")          # a restart voids leases (other client, not the handle)
        released_out_of_band.append(lease)
        state["restarted"] = True
        with log.open("a") as stream:
            stream.write("Server PID 2 exited (0).\nBench server child started (PID 4242).\n")
    reserved, real_reserve = [], case.reserve
    case.reserve = lambda **kw: reserved.append(real_reserve(**kw)) or reserved[-1]
    out = Outcome()
    with recorded(case, "Q3", out, preconditions="sim", limits="sim"):
        coordinated_restart(case, case.client, cfg, url, out, ops=fake_ops(tmp_path, request=hup, log=log),
                            reconnect_gesture=False, durability="volatile")
    assert _verdict(case) == "PASS", out.incidents
    assert any("old Board handle rejected" in e for e in out.evidence)   # a ContinuityError, not 'Reservation is closed'
    board = reserved[0]
    assert board.closed is True and not case.boards                      # sealed AFTER the proof, never released by us
    requests = [json.loads(line) for line in (case.directory / "requests.jsonl").read_text().splitlines()]
    assert not [r for r in requests if r.get("method") == "DELETE"], "the stale handle was released by the case"
    assert (case.directory / "abandoned-handles.jsonl").exists() and released_out_of_band
    assert [w for w in (case.directory / "windows.jsonl").read_text().splitlines() if '"opened"' in w]   # fresh window

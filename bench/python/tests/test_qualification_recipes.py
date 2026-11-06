"""Software checks of the versioned qualification recipes. SIMULATION ONLY: no board, no grant,
no real bench server. Hardware scenarios (Q1/Q2/Q3/C3) are NOT executed by this file."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
from qualification_campaign import restart                      # noqa: E402
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
    assert "6 skipped" in run.stdout and "passed" not in run.stdout
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
    env = {**os.environ, "BENCH_PORT": str(port), "BENCH_STATE_DIR": str(state), "BENCH_TOKENS_FILE": str(tokens)}
    process = subprocess.Popen([sys.executable, str(BENCH / "tools/run_server.py"), "--shutdown-timeout", "10"],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (tmp_path / "launcher.pid").write_text(str(process.pid))
    from iobewi_bench import BenchClient
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
        yield port, state, tmp_path / "launcher.pid", client, process
    finally:
        process.terminate()
        process.wait(timeout=20)


def test_launcher_identification_and_coordinated_restart_helpers(isolated_launcher, tmp_path):
    port, state, pid_file, client, process = isolated_launcher
    before = client.health()
    assert before["durability"] == "sqlite-full"
    launcher = restart.identify_launcher(pid_file, port)
    assert launcher.pid == process.pid
    fingerprint = restart.storage_fingerprint(state)
    restart.request_restart(launcher)
    assert restart.wait_for(lambda: restart.child_gone(launcher), 25), "old child must exit first"
    fresh = restart.wait_for(lambda: restart.replacement(launcher), 25)
    assert fresh and fresh != launcher.child
    after = restart.wait_for(lambda: _health(client), 25)
    assert after["epoch"] != before["epoch"] and after["durability"] == "sqlite-full"
    later = restart.storage_fingerprint(state)
    assert (later["db_inode"], later["lock_inode"]) == (fingerprint["db_inode"], fingerprint["lock_inode"])


def _health(client):
    try:
        return client.health()
    except Exception:
        return None


def test_stale_or_foreign_pid_files_are_refused(tmp_path):
    bogus = tmp_path / "launcher.pid"
    bogus.write_text(str(os.getpid()))                          # a live process that is NOT run_server.py
    with pytest.raises(restart.LauncherError):
        restart.identify_launcher(bogus, 8080)
    bogus.write_text("999999999")
    with pytest.raises(restart.LauncherError):
        restart.identify_launcher(bogus, 8080)

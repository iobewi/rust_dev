"""Software checks of the experimental stub variant integration and of the P4-STUB-VARIANT recipe. SIMULATION ONLY: a RAM simulator that DECLARES itself an esp32p4 and mimics the agent's
contract (variant name + pinned SHA-256, identity of the stub that ran). It proves decisions and evidence, never the behaviour of the real P4, the stub or esptool-js."""
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from iobewi_bench import BenchClient
from iobewi_bench.client import BenchError, BenchHTTPError

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
sys.path.insert(0, str(BENCH))
from protocol import STUB_VARIANTS, Fault, validate_command                         # noqa: E402
from qualification_campaign.session import SessionError, validate_session          # noqa: E402
from qualification_campaign.stub_variant import compare                            # noqa: E402

RECIPE = "recipes/qualification_campaign/test_pc_stub_variant.py"
MIB = 1024 * 1024
ASSETS = "ab" * 32
NAME = "p4-no-clock-boost"
PIN = STUB_VARIANTS[NAME]["sha256"]
ROM_MD5 = hashlib.md5((0x2000 // 4096).to_bytes(4, "little") * 1024).hexdigest()       # what the simulated flash holds at 0x2000
SCENARIO = "P4-STUB-VARIANT"
BASE_ENV = {"SIM_FLASH_MIB": "32", "SIM_FLASH_PATTERN": "sector-id", "SIM_JEDEC": "1", "SIM_ASSETS_SHA256": ASSETS, "SIM_IDENTIFY_EVENTS": "1", "SIM_CONSOLE_EVENT": "1"}
DEVICE = {"capabilities": ["identify", "flash.read", "flash.md5", "flash.write", "flash.erase", "partitions", "console"], "flash_size": 32 * MIB, "chip": "esp32p4"}
V = {"stub_variant": NAME, "stub_sha256": PIN}


def valid(**change):
    base = {"version": 1, "board_id": "board", "chip": "esp32p4", "flash_size": 32 * MIB, "transport": "external_uart",
            "partition_table_offset": None, "evidence_root": "/private/evidence", "accept_unknown_transport": True}
    return {**base, **change}


# --- the pins, in step in the three places -------------------------------------------------------------------------------------------------
def test_the_server_registry_the_agent_registry_and_the_shipped_file_carry_the_same_pins():
    shipped = (BENCH / "web/stub-esp32p4-noboost.json").read_bytes()
    js = (BENCH / "web/stub-variants.js").read_text()
    assert hashlib.sha256(shipped).hexdigest() == PIN
    assert re.search(rf"sha256: '{PIN}'", js), "the agent's registry pins another file"
    text = json.loads(shipped)["text"]
    assert re.search(rf"text_sha256: '{hashlib.sha256(text.encode()).hexdigest()}'", js)
    assert STUB_VARIANTS[NAME]["chip"] == "esp32p4"
    patch = (BENCH / "experiments/p4-stub-no-clock-boost/p4-uart-no-clock-boost.patch").read_bytes()
    assert re.search(rf"patch_sha256: '{hashlib.sha256(patch).hexdigest()}'", js)


# --- protocol validation (pure) -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["flash.md5", "flash.read"])
def test_a_variant_is_accepted_on_a_p4_for_read_commands_and_the_default_is_unchanged(command):
    base = {"address": 0x2000, "size": 4096}
    validate_command(command, base, DEVICE, {})                                   # no variant: the official stub, as before
    validate_command(command, {**base, **V}, DEVICE, {})
    validate_command(command, {**base, **V, "return_console": True}, DEVICE, {})


@pytest.mark.parametrize("bad", [{"stub_variant": NAME}, {"stub_sha256": PIN}, {**V, "stub_sha256": "0" * 64}, {**V, "stub_sha256": PIN.upper()}, {**V, "stub_variant": "nope"},
                                 {**V, "stub_variant": "__proto__"}, {**V, "stub_variant": 5}, {**V, "stub_variant": None}, {**V, "stub_sha256": 5}, {**V, "loader": "rom"}])
def test_a_variant_needs_its_name_and_the_pinned_sha256_together(bad):
    with pytest.raises(Fault):
        validate_command("flash.md5", {"address": 0x2000, "size": 4096, **bad}, DEVICE, {})


@pytest.mark.parametrize("chip", ["esp32c3", "esp32c6", "esp32s3", "esp32", "esp32p4-rev1", ""])
def test_the_variant_cannot_be_requested_for_another_chip(chip):
    with pytest.raises(Fault) as error:
        validate_command("flash.md5", {"address": 0x2000, "size": 4096, **V}, {**DEVICE, "chip": chip}, {})
    assert error.value.code == "stub_variant_chip_mismatch"


@pytest.mark.parametrize("command,extra", [("flash.write", {"chip": "esp32p4", "images": []}), ("flash.erase", {"chip": "esp32p4"}), ("partitions", {}), ("identify", {}),
                                           ("console", {}), ("uart.write", {"data": "AA=="}), ("mark", {"label": "x"}), ("reset", {})])
def test_no_command_but_the_two_reads_accepts_a_variant_so_no_write_or_erase_path_exists(command, extra):
    with pytest.raises(Fault):
        validate_command(command, {**extra, **V}, DEVICE, {})


def test_a_quarantined_device_never_gets_a_variant():
    with pytest.raises(Fault) as error:
        validate_command("flash.read", {"address": 0x2000, "size": 4096, **V}, {**DEVICE, "quarantined": True}, {})
    assert error.value.code == "device_quarantined"


@pytest.mark.parametrize("command", ["flash.md5", "flash.read"])
def test_uart_baud_accepts_only_115200_for_the_two_reads_and_the_default_is_unchanged(command):
    base = {"address": 0x2000, "size": 4096}
    validate_command(command, base, DEVICE, {})                                   # no argument: unchanged
    validate_command(command, {**base, "uart_baud": 115200}, DEVICE, {})
    validate_command(command, {**base, **V, "uart_baud": 115200}, DEVICE, {})
    for bad in (460800, 921600, 9600, 0, -1, "115200", True, None, 115200.0):
        with pytest.raises(Fault):
            validate_command(command, {**base, "uart_baud": bad}, DEVICE, {})
    with pytest.raises(Fault):
        validate_command("flash.md5", {**base, "uart_baud": 115200, "loader": "rom"}, DEVICE, {})


def test_uart_baud_is_accepted_on_flash_write_only_with_115200():
    sha = "ab" * 32
    artifacts = {"a" * 32: {"sha256": sha, "size": 4096}}
    base = {"chip": "esp32p4", "images": [{"artifact": "a" * 32, "address": 0x800000, "sha256": sha}]}
    validate_command("flash.write", base, DEVICE, artifacts)                                            # default unchanged
    validate_command("flash.write", {**base, "uart_baud": 115200, "return_console": True}, DEVICE, artifacts)
    for bad in (460800, "115200", True, 0, None):
        with pytest.raises(Fault):
            validate_command("flash.write", {**base, "uart_baud": bad}, DEVICE, artifacts)


@pytest.mark.parametrize("command", ["flash.erase", "partitions", "identify", "console", "uart.write", "mark", "reset"])
def test_uart_baud_exists_on_no_other_command(command):
    with pytest.raises(Fault):
        validate_command(command, {"uart_baud": 115200}, DEVICE, {})


# --- session ----------------------------------------------------------------------------------------------------------------------
def test_the_session_section_is_strict_and_the_consent_is_separate():
    ok = {"variant": NAME, "expected_sha256": PIN, "expected_assets_sha256": ASSETS, "reference_rom_md5": ROM_MD5}
    assert validate_session(valid(stub_variant=ok))["stub_variant"] == ok
    assert validate_session(valid())["stub_variant"] == {}
    for bad in ({"variant": "nope"}, {"expected_sha256": "A" * 64}, {"expected_sha256": "a" * 63}, {"reference_rom_md5": "g" * 32}, {"reference_rom_md5": "a" * 31}, {"unknown": 1}, "x"):
        with pytest.raises(SessionError):
            validate_session(valid(stub_variant=bad))
    assert validate_session(valid())["consent"]["scenarios"] == []
    assert validate_session(valid(consent={"scenarios": [SCENARIO]}))["consent"]["scenarios"] == [SCENARIO]


# --- pure comparison ---------------------------------------------------------------------------------------------------------------
def obs(md5s=("a", "a", "a"), read=("a", "a"), stub=("a", "a"), variant=None, ops=5, baud=115200):
    ident = {"variant": variant or "official", "verified": variant is not None, "uart_baud": baud, **({"file_sha256": PIN} if variant else {})}
    h = lambda c: c * 32                                                         # noqa: E731
    rows = []
    for n, kind in enumerate(("chip_md5", "read", "chip_md5", "read", "chip_md5")):
        if kind == "chip_md5":
            rows.append({"kind": kind, "index": n, "md5": h(md5s[n // 2]), "ram_load_ops": ops, "stub_identity": ident})
        else:
            rows.append({"kind": kind, "index": n, "md5_client": h(read[n // 2]), "stub": {"state": "received", "md5": h(stub[n // 2])}, "stub_identity": ident})
    return rows


def run(official, variant, ref=None, **extra):
    return compare(official, variant, variant_name=NAME, expected_sha256=PIN, reference_md5=ref, **extra)


def test_rows_of_the_comparison():
    unstable = obs(md5s=("a", "b", "a"), read=("c", "d"), stub=("c", "d"))
    assert run(unstable, obs(variant=NAME), "a" * 32)["rows"] == ["SV-A"]
    assert run(obs(), obs(variant=NAME), "a" * 32)["rows"] == ["SV-B"]
    assert run(unstable, obs(md5s=("a", "b", "a"), read=("c", "d"), stub=("c", "d"), variant=NAME))["rows"] == ["SV-C"]
    assert run(obs(), obs(variant=NAME), "f" * 32)["rows"] == ["SV-E"]
    assert all(f["level"] == "hypothesis" for f in run(unstable, obs(variant=NAME))["findings"] if f["id"].startswith("SV-"))


@pytest.mark.parametrize("variant", [obs(variant=None), obs(variant="other"), obs(variant=NAME, ops=0), obs(variant=NAME)[:4], obs(variant=NAME, baud=460800)])
def test_a_wrong_identity_speed_no_upload_or_missing_measurement_is_incomplete_never_a_success(variant):
    result = run(obs(), variant)
    assert result["conclusion"] == "incomplete" and result["rows"] == ["SV-D"]


def test_the_two_series_must_run_at_the_same_speed():
    result = run(obs(baud=460800), obs(variant=NAME))                              # the official series at another speed: the comparison is refused
    assert result["rows"] == ["SV-D"] and any("same speed" in f["text"] for f in result["findings"])


def test_the_official_series_must_say_official():
    bad = obs(variant=NAME)                                                       # a variant series passed as the official one
    assert run(bad, obs(variant=NAME))["rows"] == ["SV-D"]


def sector(n=2, flips=()):
    data = bytearray((n).to_bytes(4, "little") * 1024)
    for position, mask in flips:
        data[position] ^= mask
    return bytes(data)


def test_the_byte_comparison_uses_the_read_that_equals_the_rom_md5_as_truth_and_reports_positions_and_chunk_lanes_only():
    truth = sector()
    ref = hashlib.md5(truth).hexdigest()
    wrong_late = sector(flips=[(0x102, 0x02), (0x122, 0x02), (0x142, 0x04)])      # positions 2 of their chunk, beyond the first 32 bytes
    result = run(obs(), obs(variant=NAME), ref, reads={"official": [wrong_late, truth], NAME: [truth, truth]})
    section = result["bytes"]
    assert section["truth_available"] is True
    official = section["series"]["official"]
    assert official[0]["equals_truth"] is False and official[0]["deviations"] == 3 and official[0]["late_all_in_lanes_1_3"] is True and official[0]["lane_mod16"] == {2: 3}
    assert official[1]["equals_truth"] is True and all(r["equals_truth"] for r in section["series"][NAME])
    assert any(f["id"] == "lanes-official" for f in result["findings"])
    assert not any("bytes" in str(v) and "content" in str(v) for v in official), "positions and counts only"


def test_without_a_read_equal_to_the_rom_md5_nothing_is_said_about_correctness():
    result = run(obs(), obs(variant=NAME), "e" * 32, reads={"official": [sector()], NAME: [sector()]})
    assert result["bytes"]["truth_available"] is False and result["bytes"]["series"]["official"][0]["equals_truth"] is None


# --- simulated bench -----------------------------------------------------------------------------------------------------------------
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


def raw_submit(url, token, args, command="flash.md5", **flags):
    client = BenchClient(url, token)
    board = client.reserve("board", ttl=600)
    try:
        return client._request("POST", "/api/v1/operations", {"lease": board.lease_id, "command": command, "args": args, "timeout": 30, "ttl": 10, "allow_destructive": False, **flags})
    finally:
        board.close()


def test_the_client_refuses_a_variant_without_the_per_command_consent_and_for_anything_but_reads(sim):
    url, client = sim()
    board = client.reserve("board", ttl=600)
    try:
        with pytest.raises(BenchError, match="per-command consent"):
            board.command("flash.md5", {"address": 0x2000, "size": 4096, **V})
        with pytest.raises(BenchError, match="read commands only"):
            board.command("partitions", {"address": 0x8000, **V}, allow_stub_variant=True)
        assert not board.pending and not board.uncertain, "nothing was submitted"
    finally:
        board.close()


def test_the_server_demands_the_opt_in_and_the_programming_scope_and_refuses_the_manual_interface(sim):
    url, _ = sim()
    args = {"address": 0x2000, "size": 4096, **V}
    with pytest.raises(BenchHTTPError) as error:
        raw_submit(url, "pytest-client-test-token", args)                                          # no allow_stub_variant
    assert error.value.code == "stub_variant_consent_required"
    with pytest.raises(BenchHTTPError) as error:
        raw_submit(url, "pytest-second-test-token", args, allow_stub_variant=True)                 # control scope but no program scope
    assert error.value.code == "program_permission_required"
    with pytest.raises(BenchHTTPError) as error:
        raw_submit(url, "pytest-client-test-token", {**args, "stub_sha256": "0" * 64}, allow_stub_variant=True)
    assert error.value.code == "stub_variant_sha_mismatch"
    client = BenchClient(url, "pytest-client-test-token")
    device = client.device("board")
    with pytest.raises(BenchHTTPError) as error:
        client._request("POST", "/api/v1/manual-operations", {"device": "board", "generation": device["generation"], "command": "flash.md5", "args": args})
    assert error.value.code == "stub_variant_not_allowed"


# --- the recipe against the simulator -----------------------------------------------------------------------------------------------------
def run_recipe(tmp_path, url, session=None, *flags, ephemeral_ok=True):
    document = {**valid(), "evidence_root": str(tmp_path / "evidence"), "operator_timeout_s": 40, "operation_timeout_s": 60, "consent": {"scenarios": [SCENARIO]},
                "stub_variant": {"variant": NAME, "expected_sha256": PIN, "expected_assets_sha256": ASSETS, "reference_rom_md5": ROM_MD5}, **(session or {})}
    (tmp_path / "session.json").write_text(json.dumps(document))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json"), "PYTHONPATH": str(BENCH / "python/src")}
    run = subprocess.run([sys.executable, "-m", "pytest", RECIPE, "--bench", "--qual-allow-dirty", *(["--qual-allow-ephemeral-evidence"] if ephemeral_ok else []), *flags,
                          "--bench-results", str(tmp_path / "runs"), "-q", "-rs", "-p", "no:cacheprovider", "--tb=short"], cwd=BENCH, env=env, capture_output=True, text=True, timeout=240)
    reports = {}
    for path in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(path.read_text())["tests"]:
            reports[item["id"]] = item
    return run, reports


def case_dir(tmp_path):
    return next((tmp_path / "evidence").glob("PC-10-*"))


def commands(tmp_path):
    return [json.loads(line) for line in (case_dir(tmp_path) / "board-0" / "commands.jsonl").read_text().splitlines()]


def submitted(tmp_path):
    rows = [json.loads(line) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows if r["method"] == "POST" and r["path"] == "/api/v1/operations"]


def comparison(tmp_path):
    return json.loads((case_dir(tmp_path) / "stub-variant-comparison.json").read_text())


def test_stable_variant_equal_to_the_rom_md5_passes_with_ten_read_only_operations_at_the_same_speed_and_the_identity_in_the_evidence(sim, tmp_path):
    url, client = sim({"SIM_INTEGRITY": "flash"})                               # the official stub is unstable in this simulation, the variant is stable
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-10"]["status"] == "PASS", run.stdout[-1800:] + run.stderr[-300:]
    cmds = commands(tmp_path)
    assert [c["command"] for c in cmds] == ["flash.md5", "flash.read", "flash.md5", "flash.read", "flash.md5"] * 2, "reads only, ten operations, no write, no erase, no explicit console"
    official, variant = cmds[:5], cmds[5:]
    assert all("stub_variant" not in c["args"] and "stub_sha256" not in c["args"] for c in official), "the official stub is selected by default: no variant argument"
    assert all(c["args"]["uart_baud"] == 115200 for c in cmds), "the SAME speed for both series"
    assert all(c["args"]["stub_variant"] == NAME and c["args"]["stub_sha256"] == PIN for c in variant)
    assert all(c["args"]["return_console"] is True for c in cmds), "one loader session per command"
    result = comparison(tmp_path)
    assert result["rows"] == ["SV-A"] and result["variant_equals_reference"] is True and result["official_equals_reference"] is False
    assert result["bytes"]["truth_available"] is True and all(r["equals_truth"] for r in result["bytes"]["series"][NAME])
    assert any(not r["equals_truth"] for r in result["bytes"]["series"]["official"]), "the unstable official reads deviate from the ROM-matching bytes"
    assert json.loads((case_dir(tmp_path) / "official-1-read.json").read_text())["stub_identity"]["uart_baud"] == 115200
    for name in ("official-0-chip_md5", "official-1-read"):
        assert json.loads((case_dir(tmp_path) / f"{name}.json").read_text())["stub_identity"]["variant"] == "official"
    variant_record = json.loads((case_dir(tmp_path) / f"{NAME}-0-chip_md5.json").read_text())
    assert variant_record["stub_identity"]["verified"] is True and variant_record["stub_identity"]["file_sha256"] == PIN and variant_record["ram_load_ops"] > 0
    assert any("stub=p4-no-clock-boost" in line for line in reports["PC-10"]["evidence"])
    assert client.artifacts()["artifacts"] == [] and client.device("board")["reservation"] is None
    # traceability: the final state is recorded after the reservation was released, and everything is private
    after = json.loads((case_dir(tmp_path) / "after.json").read_text())
    assert after["device"]["reservation"] is None and after["device"]["connected"] is True
    modes = {str(p.relative_to(case_dir(tmp_path))): stat.S_IMODE(p.stat().st_mode) for p in case_dir(tmp_path).rglob("*")}
    assert modes and all(m == (0o700 if (case_dir(tmp_path) / n).is_dir() else 0o600) for n, m in modes.items()), {n: oct(m) for n, m in modes.items() if m not in (0o600, 0o700)}


def test_an_unstable_variant_series_fails_with_the_not_removed_row(sim, tmp_path):
    url, _ = sim({"SIM_INTEGRITY": "flash", "SIM_VARIANT_MODE": "vary"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-10"]["status"] == "FAIL" and comparison(tmp_path)["rows"] == ["SV-C"]


def test_a_stable_variant_that_disagrees_with_the_rom_md5_fails(sim, tmp_path):
    url, _ = sim()
    run, reports = run_recipe(tmp_path, url, {"stub_variant": {"variant": NAME, "expected_sha256": PIN, "expected_assets_sha256": ASSETS, "reference_rom_md5": "f" * 32}})
    assert reports["PC-10"]["status"] == "FAIL" and comparison(tmp_path)["rows"] == ["SV-E"]


def test_without_a_reference_two_stable_series_pass_and_say_nothing_can_be_compared(sim, tmp_path):
    url, _ = sim()
    run, reports = run_recipe(tmp_path, url, {"stub_variant": {"variant": NAME, "expected_sha256": PIN, "expected_assets_sha256": ASSETS}})
    assert reports["PC-10"]["status"] == "PASS" and comparison(tmp_path)["rows"] == ["SV-B"]


def test_a_wrong_pin_in_the_session_is_refused_by_the_server_after_the_official_series_and_nothing_is_replayed(sim, tmp_path):
    url, client = sim()
    run, reports = run_recipe(tmp_path, url, {"stub_variant": {"variant": NAME, "expected_sha256": "0" * 64, "expected_assets_sha256": ASSETS}})
    assert reports["PC-10"]["status"] == "FAIL"
    ops = {json.loads(line)["id"]: json.loads(line)["command"] for line in (case_dir(tmp_path) / "board-0" / "operations.jsonl").read_text().splitlines()}
    assert sorted(ops.values()) == ["console", "flash.md5", "flash.md5", "flash.md5", "flash.read", "flash.read"], "five official operations and the console return: the refused variant request never became an operation"
    assert [c["command"] for c in commands(tmp_path)].count("flash.md5") == 4, "the refused request was recorded as asked (one more flash.md5) but no operation exists for it"
    assert "stub_variant_sha_mismatch" in run.stdout + " ".join(reports["PC-10"]["incidents"] + reports["PC-10"]["evidence"])
    assert client.device("board")["reservation"] is None


def test_a_variant_that_does_not_start_stops_the_run_with_the_raw_diagnostic(sim, tmp_path):
    url, client = sim({"SIM_VARIANT_START": "fail"})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-10"]["status"] == "FAIL"
    failure = json.loads(next(case_dir(tmp_path).rglob(f"{NAME}-0-chip_md5-failure.json")).read_text())["error"]
    assert failure["code"] == "stub_variant_start_failed"
    assert [c["command"] for c in commands(tmp_path)][-2:] == ["flash.md5", "console"], "stopped at the first failing operation, one console return, no retry"
    assert client.device("board")["reservation"] is None and not client.device("board")["quarantined"]


def test_an_agent_with_another_pin_refuses_the_variant(sim, tmp_path):
    url, _ = sim({"SIM_VARIANT_PIN": "1" * 64})
    run, reports = run_recipe(tmp_path, url)
    assert reports["PC-10"]["status"] == "FAIL"
    assert json.loads(next(case_dir(tmp_path).rglob(f"{NAME}-0-chip_md5-failure.json")).read_text())["error"]["code"] == "stub_variant_sha_mismatch"


def test_blocked_without_consent_declarations_or_on_another_target_and_nothing_is_submitted(sim, tmp_path):
    url, _ = sim()
    cases = [({"consent": {"scenarios": ["P4-FLASH-INTEGRITY", "P4-ROM-MD5"]}}, "consent"), ({"stub_variant": {}}, "declared"),
             ({"stub_variant": {"variant": NAME, "expected_sha256": PIN, "expected_assets_sha256": "cd" * 32}}, "assets"), ({"chip": "esp32c6", "flash_size": 8 * MIB}, "target")]
    for n, (session, label) in enumerate(cases):
        folder = tmp_path / f"case{n}"
        folder.mkdir()
        run, reports = run_recipe(folder, url, session)
        assert "BLOCKED" in run.stdout and not submitted(folder), label

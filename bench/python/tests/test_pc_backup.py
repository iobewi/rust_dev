"""Software checks of the full-flash backup of the ESP32-P4 (G1, PC-BACKUP). SIMULATION ONLY: a RAM simulator that DECLARES itself an esp32p4 with
32 MiB and a recognisable content (every 4-KiB sector holds its own index). It proves the recipe's decisions, the verification of a backup and the
handling of faults. It proves NOTHING about the real card, the CH343, the loader or esptool-js."""
import hashlib
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

import pytest

from iobewi_bench import BenchClient, Orchestrator
from iobewi_bench.client import BenchError, ContinuityError
from iobewi_bench.qualification import Campaign

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
from qualification_campaign import backup                                              # noqa: E402
from qualification_campaign.session import SessionError, build_manifest, validate_session   # noqa: E402

RECIPES = "recipes/qualification_campaign"
MIB = 1024 * 1024
P4 = 32 * MIB


def expected_flash(size):
    return b"".join(i.to_bytes(4, "little") * 1024 for i in range(size // 4096))


def valid(**change):
    base = {"version": 1, "board_id": "board", "chip": "esp32p4", "flash_size": P4, "transport": "external_uart",
            "partition_table_offset": None, "evidence_root": "/private/evidence", "accept_unknown_transport": True}
    return {**base, **change}


# --- plan and limits ------------------------------------------------------------------------------------------------
def test_the_plan_covers_the_flash_exactly_once_in_address_order():
    plan = backup.plan_segments(P4, 4 * MIB)
    assert len(plan) == 8 and plan[0] == (0, 0, 4 * MIB) and plan[-1] == (7, 28 * MIB, 4 * MIB)
    assert sum(size for _, _, size in plan) == P4
    assert [a for _, a, _ in plan] == sorted({a for _, a, _ in plan})
    assert all(a + s == b for (_, a, s), (_, b, _) in zip(plan, plan[1:]))


def test_the_last_segment_may_be_shorter_and_bad_arguments_are_refused():
    plan = backup.plan_segments(10 * 4096, 4 * 4096)
    assert [(a, s) for _, a, s in plan] == [(0, 16384), (16384, 16384), (32768, 8192)]
    for total, segment in ((0, 4096), (4096, 0), (4096, 100), (-1, 4096), (4096.0, 4096)):
        with pytest.raises(backup.BackupError):
            backup.plan_segments(total, segment)


@pytest.mark.parametrize("section,ok", [({}, True), ({"segment_size": 16 * MIB}, True), ({"segment_size": 4096}, True),
                                        ({"segment_size": 16 * MIB + 4096}, False), ({"segment_size": 5000}, False), ({"segment_size": 0}, False),
                                        ({"return_console": "yes"}, False), ({"unknown": 1}, False), ({"segment_size": True}, False)])
def test_backup_section_respects_the_artifact_limit(section, ok):
    if ok:
        assert validate_session(valid(backup=section))["backup"]["segment_size"] <= 16 * MIB
    else:
        with pytest.raises(SessionError):
            validate_session(valid(backup=section))


def test_defaults_are_four_mib_segments_and_one_loader_session_per_segment():
    assert validate_session(valid())["backup"] == {"segment_size": 4 * MIB, "return_console": True}


# --- verification of a backup on disk -------------------------------------------------------------------------------------
def make_backup(tmp_path, total=16 * 4096, segment=4 * 4096):
    data = expected_flash(total)
    rows = []
    for index, address, size in backup.plan_segments(total, segment):
        name = backup.segment_name(index)
        (tmp_path / name).write_bytes(data[address:address + size])
        rows.append({"index": index, "address": address, "size": size, "sha256": hashlib.sha256(data[address:address + size]).hexdigest(),
                     "operation": f"op-{index}", "file": name})
    return data, backup.build_manifest(tmp_path, total, segment, rows, epoch="e", generation="g", device="board")


def test_a_complete_backup_verifies_assembles_and_has_the_global_digest(tmp_path):
    data, manifest = make_backup(tmp_path)
    assert backup.verify_backup(tmp_path, manifest, expected_total=len(data)) == hashlib.sha256(data).hexdigest()
    manifest["global_sha256"] = backup.assemble(tmp_path, manifest)
    manifest["assembled"] = "full-flash.bin"
    assert (tmp_path / "full-flash.bin").read_bytes() == data
    assert backup.verify_backup(tmp_path, manifest, expected_total=len(data)) == manifest["global_sha256"] == hashlib.sha256(data).hexdigest()


def test_assemble_never_replaces_existing_evidence(tmp_path):
    data, manifest = make_backup(tmp_path)
    (tmp_path / "full-flash.bin").write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        backup.assemble(tmp_path, manifest)
    assert (tmp_path / "full-flash.bin").read_bytes() == b"precious"


@pytest.mark.parametrize("mutate,message", [
    (lambda m, d: m["segments"].pop(2), "missing segment"),
    (lambda m, d: m["segments"].append(dict(m["segments"][1])), "duplicated segment|extra"),
    (lambda m, d: m["segments"][2].update(operation=m["segments"][1]["operation"]), "same producing operation"),
    (lambda m, d: m["segments"][1].update(address=m["segments"][1]["address"] + 4096), "differ from the plan"),
    (lambda m, d: m["segments"][1].update(size=m["segments"][1]["size"] - 4096), "differ from the plan"),
    (lambda m, d: m["segments"][1].update(index=7), "missing segment"),
    (lambda m, d: m["segments"][0].update(sha256="0" * 64), "SHA-256 differs"),
    (lambda m, d: m["segments"][0].update(file="../x.bin"), "plain name"),
    (lambda m, d: m.update(total_size=m["total_size"] - 4096), "differs from the expected flash size"),
    (lambda m, d: m.update(global_sha256="0" * 64), "global SHA-256 differs"),
    (lambda m, d: m["segments"][3].update(operation=None), "same producing operation|none"),
    (lambda m, d: m.update(version=2), "version")])
def test_every_inconsistency_is_refused(tmp_path, mutate, message):
    data, manifest = make_backup(tmp_path)
    mutate(manifest, data)
    with pytest.raises(backup.BackupError, match=message):
        backup.verify_backup(tmp_path, manifest, expected_total=len(data))


def test_a_segment_file_altered_on_disk_is_detected_even_when_the_manifest_is_unchanged(tmp_path):
    data, manifest = make_backup(tmp_path)
    path = tmp_path / manifest["segments"][2]["file"]
    raw = bytearray(path.read_bytes())
    raw[100] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(backup.BackupError, match="segment 2: SHA-256 differs"):
        backup.verify_backup(tmp_path, manifest, expected_total=len(data))


def test_a_truncated_or_missing_segment_file_is_detected(tmp_path):
    data, manifest = make_backup(tmp_path)
    (tmp_path / manifest["segments"][1]["file"]).write_bytes(b"short")
    with pytest.raises(backup.BackupError, match="holds 5 bytes"):
        backup.verify_backup(tmp_path, manifest, expected_total=len(data))
    (tmp_path / manifest["segments"][1]["file"]).unlink()
    with pytest.raises(backup.BackupError, match="file missing"):
        backup.verify_backup(tmp_path, manifest, expected_total=len(data))


def test_an_assembled_image_that_differs_is_refused(tmp_path):
    data, manifest = make_backup(tmp_path)
    manifest["global_sha256"] = backup.assemble(tmp_path, manifest)
    manifest["assembled"] = "full-flash.bin"
    raw = bytearray((tmp_path / "full-flash.bin").read_bytes())
    raw[0] ^= 1
    (tmp_path / "full-flash.bin").write_bytes(bytes(raw))
    with pytest.raises(backup.BackupError, match="assembled image differs"):
        backup.verify_backup(tmp_path, manifest, expected_total=len(data))


def test_free_space_is_checked_without_creating_anything(tmp_path):
    target = tmp_path / "not" / "created"
    assert backup.free_space_problem(target, 1) is None
    assert "needed" in backup.free_space_problem(target, 1 << 60)
    assert not (tmp_path / "not").exists()


# --- simulated bench ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def sim():
    started = []

    def start(chip="esp32p4", env=None, script=(), script_start="connect"):
        process = subprocess.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--chip", chip, "--script", json.dumps(list(script)),
                                    "--script-start", script_start], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, **(env or {})})
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


FULL = {"SIM_FLASH_MIB": "32", "SIM_FLASH_PATTERN": "sector-id"}


def run_backup(tmp_path, url, session=None, *flags, timeout=400):
    document = {**valid(), "evidence_root": str(tmp_path / "evidence"), "operator_timeout_s": 40, "operation_timeout_s": 60,
                "consent": {"scenarios": ["PC-BACKUP"]}, **(session or {})}
    (tmp_path / "session.json").write_text(json.dumps(document))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json")}
    run = subprocess.run([sys.executable, "-m", "pytest", f"{RECIPES}/test_pc_backup.py", "--bench", "--qual-allow-dirty", "--qual-allow-ephemeral-evidence",
                          *flags, "--bench-results", str(tmp_path / "runs"), "-q", "-rs", "-p", "no:cacheprovider", "--tb=short"],
                         cwd=BENCH, env=env, capture_output=True, text=True, timeout=timeout)
    reports = {}
    for path in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(path.read_text())["tests"]:
            reports[item["id"]] = item
    return run, reports


def case_dir(tmp_path):
    return next((tmp_path / "evidence").glob("PC-5-*"))


def operations(tmp_path):
    ops = [json.loads(line) for path in (tmp_path / "evidence").glob("*/board-*/operations.jsonl") for line in path.read_text().splitlines()]
    return [o["command"] for o in ops if o["state"] in ("completed", "failed", "indeterminate") and o.get("settled", True) or o["state"] == "indeterminate"]


def submitted(tmp_path):
    rows = [json.loads(line) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows if r["method"] == "POST" and r["path"] == "/api/v1/operations"]


# --- the recipe against the simulator --------------------------------------------------------------------------------------
@pytest.mark.slow_integration
def test_a_complete_p4_backup_is_read_verified_assembled_and_leaves_the_card_unchanged(sim, tmp_path):
    url, client = sim(env=FULL)
    run, reports = run_backup(tmp_path, url)
    assert reports["PC-5"]["status"] == "PASS", run.stdout[-1800:] + run.stderr[-300:]
    directory = case_dir(tmp_path) / "board-0"
    manifest = json.loads((directory / "backup-manifest.json").read_text())
    assert manifest["total_size"] == P4 and len(manifest["segments"]) == 8 and manifest["segment_size"] == 4 * MIB
    image = (directory / "full-flash.bin").read_bytes()
    assert len(image) == P4 and image == expected_flash(P4), "the assembled image must hold every sector once, in order"
    assert manifest["global_sha256"] == hashlib.sha256(image).hexdigest()
    assert [s["address"] for s in manifest["segments"]] == [i * 4 * MIB for i in range(8)]
    assert len({s["operation"] for s in manifest["segments"]}) == 8
    assert backup.verify_backup(directory, manifest, expected_total=P4) == manifest["global_sha256"]
    ops = [json.loads(r["path"] and "{}") for r in submitted(tmp_path)]
    commands = [json.loads(line)["command"] for line in (directory / "commands.jsonl").read_text().splitlines()]
    assert commands == ["identify"] + ["flash.read"] * 8, "read-only, one read per segment, no console/reset/write command"
    assert all(json.loads(line)["args"]["return_console"] is True for line in (directory / "commands.jsonl").read_text().splitlines() if '"flash.read"' in line)
    assert client.artifacts()["artifacts"] == [], "no server artifact is left behind"
    assert client.device("board")["reservation"] is None and not client.device("board")["quarantined"]
    with client.reserve("board", ttl=60) as board:                               # the simulated card is exactly as it was
        assert board.read_flash(address=0, size=4096, return_console=False) == expected_flash(4096)


@pytest.mark.slow_integration
def test_the_assembled_image_is_accepted_by_the_existing_comparison_primitive(sim, tmp_path):
    """Compatibility with Orchestrator (compare is read only; restore uses the same descriptor and is not exercised here)."""
    url, client = sim(env=FULL)
    run, reports = run_backup(tmp_path, url)
    assert reports["PC-5"]["status"] == "PASS", run.stdout[-1200:]
    directory = case_dir(tmp_path) / "board-0"
    descriptor = json.loads((directory / "full-flash.json").read_text())
    assert descriptor["complete"] is True and (descriptor["address"], descriptor["size"]) == (0, P4)
    cfg = validate_session({**valid(evidence_root=str(tmp_path))})
    case = Campaign(client, "board", tmp_path / "compare-run", __file__, "T", manifest=build_manifest(cfg, "PC-BACKUP", regions=[]))
    case.start()
    board = case.reserve(ttl=300)
    result = Orchestrator(case).compare(board, directory / "full-flash.bin")
    assert result["equal"] is True and result["size"] == P4 and result["sha256"] == descriptor["sha256"]


@pytest.mark.slow_integration
def test_with_one_loader_session_the_recipe_returns_the_console_once_at_the_end(sim, tmp_path):
    url, client = sim(env=FULL)
    run, reports = run_backup(tmp_path, url, {"backup": {"segment_size": 16 * MIB, "return_console": False}})
    assert reports["PC-5"]["status"] == "PASS", run.stdout[-1500:]
    directory = case_dir(tmp_path) / "board-0"
    commands = [json.loads(line) for line in (directory / "commands.jsonl").read_text().splitlines()]
    assert [c["command"] for c in commands] == ["identify", "flash.read", "flash.read", "console"]
    assert [c["args"].get("return_console") for c in commands[1:3]] == [False, False]
    assert len(json.loads((directory / "backup-manifest.json").read_text())["segments"]) == 2


@pytest.mark.slow_integration
def test_a_read_that_fails_midway_is_a_fail_and_secured_segments_stay_but_nothing_is_assembled(sim, tmp_path):
    url, client = sim(env={**FULL, "SIM_FAIL_READ_AT": str(4 * MIB)})
    run, reports = run_backup(tmp_path, url)
    assert reports["PC-5"]["status"] == "FAIL", run.stdout[-1200:]
    directory = case_dir(tmp_path) / "board-0"
    assert (directory / "seg-0000.bin").read_bytes() == expected_flash(4 * MIB), "the segment already secured is kept"
    assert not (directory / "backup-manifest.json").exists() and not (directory / "full-flash.bin").exists()
    assert not (directory / "seg-0002.bin").exists(), "no segment is skipped to continue"
    assert len([c for c in (directory / "commands.jsonl").read_text().splitlines() if '"flash.read"' in c]) == 2, "no retry of the failed read"
    assert client.device("board")["reservation"] is None


@pytest.mark.slow_integration
def test_an_agent_that_vanishes_during_a_read_is_indeterminate_and_nothing_is_replayed(sim, tmp_path):
    url, client = sim(env={**FULL, "SIM_DROP_DURING_READ": "1"})
    run, reports = run_backup(tmp_path, url, {"operation_timeout_s": 3})
    assert reports["PC-5"]["status"] == "INDETERMINATE", run.stdout[-1500:]
    directory = case_dir(tmp_path) / "board-0"
    assert not (directory / "backup-manifest.json").exists()
    assert len(submitted(tmp_path)) == 2, "identify and ONE read: the unresolved read is never replayed"
    assert "UNKNOWN" in reports["PC-5"]["restoration"] or reports["PC-5"]["restoration"].startswith("not applicable")


@pytest.mark.slow_integration
def test_a_hanging_read_is_indeterminate_not_a_failure(sim, tmp_path):
    url, client = sim(env={**FULL, "SIM_HANG_COMMAND": "flash.read"})
    run, reports = run_backup(tmp_path, url, {"operation_timeout_s": 2})
    assert reports["PC-5"]["status"] == "INDETERMINATE", run.stdout[-1500:]
    assert len(submitted(tmp_path)) == 2


@pytest.mark.slow_integration
def test_a_generation_change_after_the_identification_breaks_the_proof_window(sim, tmp_path):
    url, client = sim(env={**FULL, "SIM_REENUMERATE_AFTER_IDENTIFY": "board:1"})
    run, reports = run_backup(tmp_path, url)
    assert reports["PC-5"]["status"] == "INDETERMINATE", run.stdout[-1500:]
    assert not list((case_dir(tmp_path) / "board-0").glob("seg-*.bin"))


@pytest.mark.slow_integration
def test_a_full_artifact_storage_is_a_fail_and_leaves_the_prefilled_artifacts_alone(sim, tmp_path):
    url, client = sim(env=FULL)
    filler = [client.upload(b"\x00" * (16 * MIB - n)) for n in range(4)]            # 64 MiB total: the server's whole artifact storage
    assert client.artifacts()["storage"]["available"] < 4 * MIB
    run, reports = run_backup(tmp_path, url)
    assert reports["PC-5"]["status"] == "FAIL", run.stdout[-1500:]
    directory = case_dir(tmp_path) / "board-0"
    assert not (directory / "backup-manifest.json").exists() and not list(directory.glob("seg-*.bin"))
    assert {a["id"] for a in client.artifacts()["artifacts"]} == {a["id"] for a in filler}, "the recipe never deletes artifacts it does not own"
    for artifact in filler:
        client.delete_artifact(artifact["id"])


# --- refusals before any evidence or command ---------------------------------------------------------------------------------
@pytest.mark.parametrize("session,why", [
    ({"consent": {"scenarios": []}}, "PC-BACKUP"),
    ({"chip": "esp32c6", "flash_size": 8 * MIB}, "backs up the esp32p4"),            # the C6 is not covered at all
    ({"flash_size": 16 * MIB}, "backs up the esp32p4 with 33554432 bytes")])
def test_the_backup_is_blocked_before_any_evidence_or_command(sim, tmp_path, session, why):
    url, client = sim(env=FULL)
    run, reports = run_backup(tmp_path, url, session)
    assert not reports and "BLOCKED" in run.stdout and why in run.stdout, run.stdout[-900:]
    assert not submitted(tmp_path)


def test_a_c6_session_is_blocked_even_with_the_consent(sim, tmp_path):
    url, client = sim(chip="esp32c6", env={"SIM_FLASH_MIB": "8"})
    run, reports = run_backup(tmp_path, url, {"chip": "esp32c6", "flash_size": 8 * MIB})
    assert not reports and "BLOCKED" in run.stdout and "backs up the esp32p4" in run.stdout
    assert not submitted(tmp_path)


def test_an_ephemeral_evidence_root_blocks_the_backup(sim, tmp_path):
    url, client = sim(env=FULL)
    (tmp_path / "session.json").write_text(json.dumps({**valid(), "evidence_root": str(tmp_path / "evidence"), "consent": {"scenarios": ["PC-BACKUP"]}}))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json")}
    probe = subprocess.run([sys.executable, "-c",
        "import sys; sys.path.insert(0,'recipes'); from qualification_campaign import evidence;"
        "evidence.check_persistent_root=lambda r:(False,'the evidence root is on an ephemeral filesystem (overlay mounted at /)',{});"
        "import pytest; sys.exit(pytest.main(['recipes/qualification_campaign/test_pc_backup.py','--bench','--qual-allow-dirty','-q','-rs','-p','no:cacheprovider']))"],
        cwd=BENCH, env=env, capture_output=True, text=True, timeout=120)
    assert "BLOCKED" in probe.stdout and "ephemeral filesystem" in probe.stdout and not submitted(tmp_path)


# --- Orchestrator.backup, in process: faults on a small flash ------------------------------------------------------------------
def small_case(sim, tmp_path, env=None):
    url, client = sim(chip="esp32s3", env={"SIM_FLASH_PATTERN": "sector-id", **(env or {})})
    cfg = validate_session({**valid(chip="esp32s3", flash_size=4 * MIB, evidence_root=str(tmp_path))})
    case = Campaign(client, "board", tmp_path / "run", __file__, "T", manifest=build_manifest(cfg, "PC-BACKUP", regions=[]))
    case.start()
    return client, case, case.reserve(ttl=300), Orchestrator(case)


def test_return_console_is_passed_to_every_read_and_defaults_to_false(sim, tmp_path):
    client, case, board, runner = small_case(sim, tmp_path)
    runner.backup(board, address=0, size=8192, filename="a.bin", segment_size=4096)
    runner.backup(board, address=0, size=8192, filename="b.bin", segment_size=4096, return_console=True)
    commands = [json.loads(line)["args"]["return_console"] for line in (board.output_dir / "commands.jsonl").read_text().splitlines()]
    assert commands == [False, False, True, True]


def test_an_epoch_change_between_segments_stops_the_backup_and_keeps_what_was_secured(sim, tmp_path):
    client, case, board, runner = small_case(sim, tmp_path)
    runner.backup(board, address=0, size=4096, filename="seg-0000.bin", segment_size=4096)
    original = board.client.health
    board.client.health = lambda: {**original(), "epoch": "another-epoch"}              # the server restarted: every handle is stale
    with pytest.raises(ContinuityError):
        runner.backup(board, address=4096, size=4096, filename="seg-0001.bin", segment_size=4096)
    assert (board.output_dir / "seg-0000.bin").read_bytes() == expected_flash(4096)
    assert not (board.output_dir / "seg-0001.bin").exists()


def test_a_server_artifact_whose_digest_differs_is_never_accepted(sim, tmp_path):
    client, case, board, runner = small_case(sim, tmp_path)
    real = board.client._request

    def corrupting(method, path, body=None, *, binary=False):
        payload = real(method, path, body, binary=binary)
        if binary and path.startswith("/api/v1/artifacts/"):
            return bytes([payload[0] ^ 0xFF]) + payload[1:]                     # one corrupted byte in transit
        return payload
    board.client._request = corrupting
    with pytest.raises(BenchError, match="SHA-256"):
        runner.backup(board, address=0, size=4096, filename="seg-0000.bin", segment_size=4096)
    assert not (board.output_dir / "seg-0000.bin").exists()


def test_a_local_copy_that_cannot_be_secured_keeps_the_server_artifact(sim, tmp_path, monkeypatch):
    client, case, board, runner = small_case(sim, tmp_path)
    from iobewi_bench import orchestration
    monkeypatch.setattr(orchestration, "_sync_file", lambda stream: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        runner.backup(board, address=0, size=4096, filename="seg-0000.bin", segment_size=4096)
    assert len(client.artifacts()["artifacts"]) == 1, "the only copy of the segment is the server artifact: it must not be deleted"


def test_artifact_limits_of_the_server_match_the_segment_rules(sim):
    url, client = sim(env=FULL)
    storage = client.artifacts()["storage"]
    assert storage["max_artifact"] == 16 * MIB == validate_session(valid(backup={"segment_size": 16 * MIB}))["backup"]["segment_size"]
    assert storage["total"] == 64 * MIB and 2 * 16 * MIB <= storage["total"], "two full-size segments fit; each is deleted once copied"
    assert (P4 + 15 * MIB) // (16 * MIB) == 2 and len(backup.plan_segments(P4, 16 * MIB)) == 2

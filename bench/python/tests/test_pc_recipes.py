"""Software checks of the generic ESP32-P4 / ESP32-C6 qualification recipes (G1). SIMULATION ONLY: a RAM simulator that DECLARES itself
an esp32p4 / esp32c6; no real board, no CH343 / CP210x, no Web Serial, no ROM. They prove the recipes' decisions (BLOCKED / PASS / FAIL),
the session rules, the image plan and the evidence-storage check. They prove NOTHING about the real cards."""
import hashlib
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from iobewi_bench import BenchClient
from iobewi_bench.client import OperationError

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
from qualification_campaign import evidence, images, pc            # noqa: E402
from qualification_campaign.harness import Blocked                  # noqa: E402
from qualification_campaign.session import SessionError, validate_session   # noqa: E402
from qualification_campaign.test_pc_cycle import sector_outside   # noqa: E402

RECIPES = "recipes/qualification_campaign"
P4_TEMPLATE = BENCH / RECIPES / "session.p4.example.json"
C6_TEMPLATE = BENCH / RECIPES / "session.c6.example.json"
MIB = 1024 * 1024


def valid(**change):
    base = {"version": 1, "board_id": "board", "chip": "esp32p4", "flash_size": 4 * MIB, "transport": "external_uart",
            "partition_table_offset": None, "evidence_root": "/private/evidence", "accept_unknown_transport": True}
    return {**base, **change}


REGION = {"address": 0x100000, "size": 0x4000}


def image(**change):
    return {"label": "app", "path": "/private/app.bin", "sha256": "a" * 64, "address": 0x100000, **change}


# --- session ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("template,chip,entry", [(P4_TEMPLATE, "esp32p4", "auto"), (C6_TEMPLATE, "esp32c6", "manual")])
def test_templates_are_valid_and_grant_nothing(template, chip, entry):
    cfg = validate_session(json.loads(template.read_text()))
    assert cfg["chip"] == chip and cfg["loader"]["entry"] == entry
    assert cfg["consent"]["scenarios"] == [] and cfg["approved_regions"] == [] and cfg["images"] == []


def test_the_two_new_chips_are_accepted_and_others_still_refused():
    assert validate_session(valid(chip="esp32c6"))["chip"] == "esp32c6"
    with pytest.raises(SessionError):
        validate_session(valid(chip="esp8266"))


@pytest.mark.parametrize("loader", [
    {"entry": "manual"},                                                     # no banner: it has no default
    {"entry": "manual", "download_marker": ""}, {"entry": "manual", "download_marker": " padded "},
    {"entry": "auto", "download_marker": "waiting"}, {"entry": "sometimes"}, {"unknown": 1},
    {"entry": "manual", "download_marker": "x", "marker_timeout_s": 4}, {"entry": "manual", "download_marker": "x", "marker_timeout_s": 301}])
def test_invalid_loader_sections_are_refused(loader):
    with pytest.raises(SessionError):
        validate_session(valid(loader=loader))


def test_manual_loader_needs_a_declared_banner_and_keeps_the_default_timeout():
    cfg = validate_session(valid(loader={"entry": "manual", "download_marker": "waiting for download"}))
    assert cfg["loader"] == {"entry": "manual", "download_marker": "waiting for download", "marker_timeout_s": 120}


def test_images_without_any_approved_region_are_refused():
    with pytest.raises(SessionError, match="approved_regions"):
        validate_session(valid(images=[image()]))


@pytest.mark.parametrize("images_value", [
    "x", [1],
    [{**image(), "extra": 1}], [image(label="bad label")], [image(label="")], [image(path="relative.bin")], [image(path="/dir/")],
    [image(sha256="A" * 64)], [image(sha256="a" * 63)], [image(address=0x100001)], [image(address=-4096)],
    [image(address=0x200000)],                                               # outside every approved region
    [image(), image()]])                                                     # duplicate label
def test_invalid_image_declarations_are_refused(images_value):
    with pytest.raises(SessionError):
        validate_session(valid(approved_regions=[REGION], images=images_value))


def test_images_cannot_exceed_sixteen_and_never_create_write_permission():
    many = [image(label=f"i{n}", address=0x100000) for n in range(17)]
    with pytest.raises(SessionError):
        validate_session(valid(approved_regions=[REGION], images=many))
    cfg = validate_session(valid(approved_regions=[REGION], images=[image()]))
    assert cfg["approved_regions"] == [REGION] and cfg["consent"]["scenarios"] == []


# --- image plan --------------------------------------------------------------------------------------------------------
def loaded(label, address, size):
    return {"label": label, "address": address, "data": b"\x5a" * size, "sha256": "0" * 64}


def test_plan_accepts_images_inside_one_region_and_rounds_to_sectors():
    rows = images.plan([loaded("boot", 0x100000, 1), loaded("app", 0x101000, 4097)], [REGION], 4 * MIB)
    assert [r["sectors"] for r in rows] == [[0x100000, 0x101000], [0x101000, 0x103000]] and {r["region"] for r in rows} == {0}


@pytest.mark.parametrize("items,regions,message", [
    ([loaded("a", 0x100000, 0x4001)], [REGION], "not inside one approved region"),               # spills into the next sector
    ([loaded("a", 0x100000, 100), loaded("b", 0x100800 // 4096 * 4096, 100)], [REGION], "overlap"),   # same sector
    ([loaded("a", 0x100000, 100), loaded("b", 0x100000, 5000)], [REGION], "overlap"),
    ([loaded("a", 0x3FF000, 0x2000)], [{"address": 0x3FF000, "size": 0x2000}], "beyond the declared flash"),
    ([loaded("a", 0x100000, 100)], [{"address": 0x200000, "size": 0x1000}], "not inside one approved region"),
    ([loaded("a", 0x103000, 100)], [{"address": 0x100000, "size": 0x2000}, {"address": 0x103000, "size": 0x1000}], None),
    ([], [REGION], "no image")])
def test_plan_refuses_what_cannot_be_programmed(items, regions, message):
    if message is None:
        assert images.plan(items, regions, 4 * MIB)[0]["region"] == 1
        return
    with pytest.raises(images.ImagePlanError, match=message):
        images.plan(items, regions, 4 * MIB)


def test_an_image_cannot_straddle_two_adjacent_regions():
    adjacent = [{"address": 0x100000, "size": 0x1000}, {"address": 0x101000, "size": 0x1000}]
    with pytest.raises(images.ImagePlanError, match="not inside one approved region"):
        images.plan([loaded("a", 0x100000, 0x1800)], adjacent, 4 * MIB)


def test_load_images_verifies_the_pinned_digest_and_the_file(tmp_path):
    path = tmp_path / "app.bin"
    path.write_bytes(b"firmware")
    item = {"label": "app", "path": str(path), "sha256": hashlib.sha256(b"firmware").hexdigest(), "address": 0x100000}
    assert images.load_images([item])[0]["data"] == b"firmware"
    with pytest.raises(images.ImagePlanError, match="SHA-256 differs"):
        images.load_images([{**item, "sha256": "0" * 64}])
    with pytest.raises(images.ImagePlanError, match="not found"):
        images.load_images([{**item, "path": str(tmp_path / "missing.bin")}])
    path.write_bytes(b"")
    with pytest.raises(images.ImagePlanError, match="1\\.\\."):
        images.load_images([{**item, "sha256": hashlib.sha256(b"").hexdigest()}])


def test_a_file_over_the_bench_limit_is_refused_without_being_read(tmp_path):
    path = tmp_path / "big.bin"
    with path.open("wb") as stream:
        stream.truncate(images.MAX_IMAGE + 1)
    with pytest.raises(images.ImagePlanError, match="1\\.\\."):
        images.load_images([{"label": "big", "path": str(path), "sha256": "0" * 64, "address": 0}])


def test_grant_budget_counts_every_program_call_of_the_cycle():
    rows = [{"size": 1000}, {"size": 2000}]
    regions = [{"address": 0, "size": 8 * MIB}, {"address": 8 * MIB, "size": 4096}]
    count, grant = images.operations(rows, regions, 120)
    assert count == 2 + 2 + 1 and grant == count * (2 * 120 + 90) + 60       # 8 MiB restores in two 4-MiB segments


def test_manifest_refusal_sector_is_found_or_reported_absent():
    assert sector_outside([REGION], 4 * MIB) == 0x104000
    assert sector_outside([{"address": 0, "size": 0x1000}], 0x2000) == 0x1000
    assert sector_outside([{"address": 0, "size": 0x2000}], 0x2000) is None


# --- evidence storage --------------------------------------------------------------------------------------------------
MOUNTINFO = r"""
2203 1900 0:51 / / rw,relatime master:1 - overlay overlay rw,lowerdir=/a,upperdir=/b
2204 2203 0:52 / /proc rw,nosuid - proc proc rw
2210 2203 8:48 /var/lib/docker/volumes/iobewi-bench-state/_data /root/.local/state/iobewi-bench rw,relatime - ext4 /dev/sdd rw
2211 2203 8:48 /var/lib/docker/volumes/iobewi-qualification-evidence/_data /root/qualification-evidence rw - ext4 /dev/sdd rw
2212 2203 0:60 / /dev/shm rw - tmpfs shm rw
2213 2203 0:61 / /mnt/with\040space rw - xfs /dev/sdx rw
2214 2210 0:62 / /root/.local/state/iobewi-bench/scratch rw - tmpfs tmpfs rw
"""


@pytest.mark.parametrize("path,ok,fragment", [
    ("/root/.local/state/iobewi-bench/qualification-evidence", True, "ext4 mounted at /root/.local/state/iobewi-bench"),
    ("/root/qualification-evidence/run-1", True, "/root/qualification-evidence"),
    ("/root/bench-evidence/s3-uart-q1-v2", False, "overlay mounted at /"),                     # the historical, ephemeral folder
    ("/tmp/evidence", False, "overlay"),
    ("/dev/shm/evidence", False, "tmpfs"),
    ("/root/.local/state/iobewi-bench/scratch/x", False, "tmpfs"),                              # a nested ephemeral mount wins over its parent
    ("/mnt/with space/evidence", True, "xfs")])
def test_persistence_is_decided_from_the_mount_table(path, ok, fragment, monkeypatch):
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    result, reason, detail = evidence.check_persistent_root(path, MOUNTINFO)
    assert result is ok and fragment in reason and detail["mount_point"]


def test_an_unknown_or_unreadable_mount_table_refuses():
    assert evidence.check_persistent_root("/x", "")[0] is False
    assert evidence.check_persistent_root("/x", "garbage\nlines only")[0] is False


def test_a_symlink_into_an_ephemeral_area_does_not_pass(tmp_path, monkeypatch):
    target, link = tmp_path / "real", tmp_path / "evidence-link"
    target.mkdir()
    link.symlink_to(target)
    mountinfo = f"1 0 0:1 / / rw - ext4 /dev/a rw\n2 1 0:2 / {target.parent} rw - tmpfs tmpfs rw\n"
    ok, reason, _ = evidence.check_persistent_root(link, mountinfo)
    assert ok is False and "tmpfs" in reason


def test_require_persistent_evidence_blocks_unless_the_development_override_is_given(monkeypatch):
    monkeypatch.setattr(evidence, "check_persistent_root", lambda root: (False, "the evidence root is on an ephemeral filesystem (overlay mounted at /)", {"resolved": root}))
    cfg = {"evidence_root": "/x"}
    with pytest.raises(pytest.skip.Exception, match="BLOCKED.*ephemeral"):
        evidence.require_persistent_evidence(cfg, SimpleNamespace(getoption=lambda name, default=None: False))
    allowed = evidence.require_persistent_evidence(cfg, SimpleNamespace(getoption=lambda name, default=None: True))
    assert allowed["override"] is True and allowed["persistent"] is False
    monkeypatch.setattr(evidence, "check_persistent_root", lambda root: (True, "persistent filesystem ext4 mounted at /v", {"resolved": root}))
    assert evidence.require_persistent_evidence(cfg, SimpleNamespace(getoption=lambda name, default=None: False))["override"] is False


def test_the_preparation_tool_refuses_a_relative_root_and_never_deletes():
    run = subprocess.run([sys.executable, str(BENCH / "tools/prepare_evidence_root.py"), "--root", "relative/dir"], capture_output=True, text=True, timeout=30)
    assert run.returncode == 1 and "absolute" in run.stderr


def test_the_preparation_tool_check_mode_creates_nothing(tmp_path):
    root = tmp_path / "never-created"
    run = subprocess.run([sys.executable, str(BENCH / "tools/prepare_evidence_root.py"), "--root", str(root), "--check"], capture_output=True, text=True, timeout=30)
    assert run.returncode in (0, 2) and not root.exists()


# --- manual loader window (pure parts) ---------------------------------------------------------------------------------
def failing(code):
    error = OperationError({"id": "op", "state": "failed", "error": {"code": code}})

    def action():
        raise error
    return action


def test_an_unreachable_loader_is_blocked_in_manual_mode_and_forgets_the_window():
    state = pc.LoaderState()
    state.opened = True
    with pytest.raises(Blocked, match="serial_timeout"):
        pc.first_command({"loader": {"entry": "manual"}}, state, failing("serial_timeout"))
    assert state.opened is False


def test_an_unreachable_loader_stays_an_error_in_automatic_mode_and_other_codes_propagate():
    with pytest.raises(OperationError):
        pc.first_command({"loader": {"entry": "auto"}}, pc.LoaderState(), failing("serial_timeout"))
    with pytest.raises(OperationError):
        pc.first_command({"loader": {"entry": "manual"}}, pc.LoaderState(), failing("physical_identity_changed"))
    assert pc.first_command({"loader": {"entry": "manual"}}, pc.LoaderState(), lambda: 7) == 7


@pytest.mark.parametrize("chip,skipped", [("esp32p4", False), ("esp32c6", False), ("esp32s3", True), ("esp32c3", True)])
def test_the_gate_targets_only_the_two_new_chips_and_needs_the_consent(chip, skipped):
    cfg = {"chip": chip, "consent": {"scenarios": ["PC-READ"]}}
    if skipped:
        with pytest.raises(pytest.skip.Exception, match="targets"):
            pc.gate(cfg, "PC-READ")
    else:
        pc.gate(cfg, "PC-READ")
        with pytest.raises(pytest.skip.Exception, match="PC-CYCLE"):
            pc.gate(cfg, "PC-CYCLE")


# --- simulated bench -----------------------------------------------------------------------------------------------------
@pytest.fixture
def sim():
    started = []

    def start(chip="esp32p4", script=(), script_start="connect", env=None):
        process = subprocess.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--script", json.dumps(list(script)),
                                    "--script-start", script_start, "--chip", chip],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, **(env or {})})
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


def run_pc(tmp_path, url, selector, session, *flags, timeout=180):
    document = {**valid(), "evidence_root": str(tmp_path / "evidence"), "operator_timeout_s": 40, **session}
    (tmp_path / "session.json").write_text(json.dumps(document))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json")}
    run = subprocess.run([sys.executable, "-m", "pytest", f"{RECIPES}/{selector}", "--bench", "--qual-allow-dirty", "--qual-allow-ephemeral-evidence",
                          *flags, "--bench-results", str(tmp_path / "runs"), "-q", "-rs", "-p", "no:cacheprovider", "--tb=short"],
                         cwd=BENCH, env=env, capture_output=True, text=True, timeout=timeout)
    reports = {}
    for path in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(path.read_text())["tests"]:
            reports[item["id"]] = item
    return run, reports


def commands(tmp_path):
    ops = [json.loads(line) for path in (tmp_path / "evidence").glob("*/board-*/operations.jsonl") for line in path.read_text().splitlines()]
    return [o["command"] for o in ops if o.get("state") == "completed" or o.get("state") == "failed"]


def submissions(tmp_path):
    rows = [json.loads(line) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows if r["method"] == "POST" and r["path"] == "/api/v1/operations"]


# --- PC-READ against the simulator ------------------------------------------------------------------------------------
@pytest.mark.parametrize("chip", ["esp32p4", "esp32c6"])
def test_pc_identity_and_reads_pass_on_either_chip_and_send_only_reads(sim, tmp_path, chip):
    url, client = sim(chip=chip)
    session = {"chip": chip, "consent": {"scenarios": ["PC-READ"]}, "readonly": {"reads": [{"address": 0x1000, "size": 4096}]}}
    run, reports = run_pc(tmp_path, url, "test_pc_read_only.py", session)
    assert "2 passed" in run.stdout and "1 skipped" in run.stdout, run.stdout[-1500:]
    assert {k: v["status"] for k, v in reports.items()} == {"PC-0": "PASS", "PC-1": "PASS"}
    assert any("loader entry: automatic" in e for e in reports["PC-0"]["evidence"])
    assert set(commands(tmp_path)) <= {"identify", "flash.read", "console"}
    assert client.device("board")["reservation"] is None


def test_a_session_for_another_chip_is_blocked_before_any_evidence_or_command(sim, tmp_path):
    url, client = sim(chip="esp32s3")
    run, reports = run_pc(tmp_path, url, "test_pc_read_only.py", {"chip": "esp32s3", "consent": {"scenarios": ["PC-READ"]}})
    assert not reports and "BLOCKED" in run.stdout and "esp32c6 or esp32p4" in run.stdout and not submissions(tmp_path)


def test_a_wrong_measured_chip_is_a_fail(sim, tmp_path):
    url, client = sim(chip="esp32c6")
    run, reports = run_pc(tmp_path, url, "test_pc_read_only.py::test_pc0_identity", {"chip": "esp32p4", "consent": {"scenarios": ["PC-READ"]}})
    assert reports["PC-0"]["status"] == "BLOCKED" and "does not match the observed card" in " ".join(reports["PC-0"]["incidents"])
    assert not submissions(tmp_path)


# --- manual ROM-loader window -------------------------------------------------------------------------------------------
STREAM = {"SIM_UART_RATE": "5:16", "SIM_UART_TAG": "1"}                 # the simulated card prints "board|" continuously


def test_manual_entry_proceeds_only_after_the_declared_banner_is_observed(sim, tmp_path):
    url, client = sim(chip="esp32c6", env=STREAM)
    session = {"chip": "esp32c6", "consent": {"scenarios": ["PC-READ"]},
               "loader": {"entry": "manual", "download_marker": "board|", "marker_timeout_s": 20}}
    run, reports = run_pc(tmp_path, url, "test_pc_read_only.py::test_pc0_identity", session)
    assert reports["PC-0"]["status"] == "PASS", run.stdout[-1200:]
    assert any("ROM banner 'board|' observed after the operator's gesture" in e for e in reports["PC-0"]["evidence"])
    window = json.loads(next((tmp_path / "evidence").glob("PC-0-*/loader-window.json")).read_text())
    assert window["download_marker"] == "board|"
    requests = (next((tmp_path / "evidence").glob("PC-0-*/requests.jsonl"))).read_text()
    assert requests.count('"path": "/api/v1/operations"') >= 1


def test_manual_entry_without_the_banner_is_blocked_and_nothing_is_submitted(sim, tmp_path):
    url, client = sim(chip="esp32c6", env=STREAM)
    session = {"chip": "esp32c6", "consent": {"scenarios": ["PC-READ"]},
               "loader": {"entry": "manual", "download_marker": "NEVER-PRINTED", "marker_timeout_s": 5}}
    run, reports = run_pc(tmp_path, url, "test_pc_read_only.py::test_pc0_identity", session)
    assert reports["PC-0"]["status"] == "BLOCKED" and "not seen within 5 s" in " ".join(reports["PC-0"]["incidents"])
    assert not submissions(tmp_path), "a loader command was submitted before the banner"
    assert client.device("board")["reservation"] is None


def test_manual_entry_with_an_unreachable_loader_is_blocked_not_failed(sim, tmp_path):
    url, client = sim(chip="esp32c6", env={**STREAM, "SIM_FAIL_COMMAND": "identify"})       # the simulated loader never answers
    session = {"chip": "esp32c6", "consent": {"scenarios": ["PC-READ"]},
               "loader": {"entry": "manual", "download_marker": "board|", "marker_timeout_s": 20}}
    run, reports = run_pc(tmp_path, url, "test_pc_read_only.py::test_pc0_identity", session)
    assert reports["PC-0"]["status"] == "BLOCKED" and "loader is not reachable (serial_timeout)" in " ".join(reports["PC-0"]["incidents"])


# --- PC-RESET ------------------------------------------------------------------------------------------------------------
def test_reset_needs_its_opt_in_and_then_sees_the_banner(sim, tmp_path):
    url, client = sim(chip="esp32p4")
    session = {"chip": "esp32p4", "consent": {"scenarios": ["PC-RESET"]}}
    run, reports = run_pc(tmp_path, url, "test_pc_reset.py", session)
    assert "--qual-allow-reset not given" in run.stdout and not reports and not submissions(tmp_path)
    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    run, reports = run_pc(tmp2, url, "test_pc_reset.py", session, "--qual-allow-reset")
    assert reports["PC-3"]["status"] == "PASS", run.stdout[-1200:]
    assert set(commands(tmp2)) == {"reset"}


# --- PC-CYCLE ------------------------------------------------------------------------------------------------------------
def cycle_session(tmp_path, *, chip="esp32p4", data=b"\xA5" * 6000, declared=None, **change):
    path = tmp_path / "app.bin"
    path.write_bytes(data)
    digest = declared or hashlib.sha256(data).hexdigest()
    return {"chip": chip, "operation_timeout_s": 20, "consent": {"scenarios": ["PC-CYCLE"]},
            "approved_regions": [{"address": 0x100000, "size": 0x4000}],
            "images": [{"label": "app", "path": str(path), "sha256": digest, "address": 0x100000}], **change}


GRANT = [[1, 1800], [14, 0]]                       # the simulated operator grants once, revokes later (explicit signal: the reservation)


@pytest.mark.slow_integration
@pytest.mark.parametrize("chip", ["esp32p4", "esp32c6"])
def test_cycle_programs_reads_back_restores_and_leaves_the_flash_as_found(sim, tmp_path, chip):
    url, client = sim(chip=chip, script=GRANT, script_start="reservation")
    run, reports = run_pc(tmp_path, url, "test_pc_cycle.py", cycle_session(tmp_path, chip=chip), "--bench-allow-destructive")
    assert reports["PC-4"]["status"] == "PASS", run.stdout[-1800:] + run.stderr[-400:]
    evidence_lines = " ".join(reports["PC-4"]["evidence"])
    assert "programmed; independent readback equal" in evidence_lines and "evidence storage" in evidence_lines
    assert reports["PC-4"]["restoration"].startswith("restored; independent compare equal")
    ops = commands(tmp_path)
    assert ops.count("flash.write") == 2                                    # one image, then one restored segment
    assert set(ops) <= {"identify", "flash.read", "flash.write", "console"}, "no reset, erase, boot.download, io0 or uart.write"
    with client.reserve("board", ttl=60) as board:
        assert board.read_flash(address=0x100000, size=0x4000, return_console=False) == b"\xff" * 0x4000
    assert client.device("board")["reservation"] is None and client.device("board")["privileged_seconds"] == 0
    assert len(submissions(tmp_path)) == len(ops), "a client-side refusal reached the bench"


@pytest.mark.parametrize("change,why,flags", [
    ({"consent": {"scenarios": []}}, "PC-CYCLE", ("--bench-allow-destructive",)),
    ({}, "--bench-allow-destructive not given", ()),
    ({"images": []}, "lacks approved_regions or images", ("--bench-allow-destructive",)),
    ({"approved_regions": []}, "lacks approved_regions or images", ("--bench-allow-destructive",))])
def test_cycle_is_blocked_before_any_evidence_or_command(sim, tmp_path, change, why, flags):
    url, client = sim()
    session = cycle_session(tmp_path)
    session.update(change)
    if not session["approved_regions"]:
        session["images"] = []                                                  # the session validator itself refuses images without regions
    run, reports = run_pc(tmp_path, url, "test_pc_cycle.py", session, *flags)
    assert not reports and "BLOCKED" in run.stdout and why in run.stdout, run.stdout[-900:]
    assert not submissions(tmp_path)
    assert not list((tmp_path / "evidence").glob("*")) if (tmp_path / "evidence").exists() else True


def test_cycle_refuses_a_changed_image_before_any_command(sim, tmp_path):
    url, client = sim()
    run, reports = run_pc(tmp_path, url, "test_pc_cycle.py", cycle_session(tmp_path, declared="0" * 64), "--bench-allow-destructive")
    assert not reports and "SHA-256 differs from the declared value" in run.stdout and not submissions(tmp_path)


def test_cycle_refuses_an_ephemeral_evidence_root_without_the_override(sim, tmp_path):
    url, client = sim()
    document = {**valid(), **cycle_session(tmp_path), "evidence_root": str(tmp_path / "evidence")}
    (tmp_path / "session.json").write_text(json.dumps(document))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json"),
           "PC_FORCE_EPHEMERAL": "1"}
    conftest_probe = subprocess.run([sys.executable, "-c",
        "import sys; sys.path.insert(0,'recipes'); from qualification_campaign import evidence;"
        "evidence.check_persistent_root=lambda r:(False,'the evidence root is on an ephemeral filesystem (overlay mounted at /)',{});"
        "import pytest; sys.exit(pytest.main(['recipes/qualification_campaign/test_pc_cycle.py','--bench','--qual-allow-dirty',"
        "'--bench-allow-destructive','-q','-rs','-p','no:cacheprovider']))"],
        cwd=BENCH, env=env, capture_output=True, text=True, timeout=120)
    assert "BLOCKED" in conftest_probe.stdout and "ephemeral filesystem" in conftest_probe.stdout, conftest_probe.stdout[-900:]
    assert not submissions(tmp_path)


@pytest.mark.slow_integration
def test_an_image_already_on_the_card_is_blocked_after_the_backup_and_before_any_write(sim, tmp_path):
    data = b"\xC3" * 5000
    url, client = sim(script=GRANT, script_start="reservation")
    deadline = time.monotonic() + 15
    with client.reserve("board", ttl=60, allow_destructive=True) as board:                        # put the image on the simulated card first
        while client.device("board")["privileged_seconds"] <= 0:                                  # wait for the simulated operator's grant
            assert time.monotonic() < deadline
            time.sleep(0.05)
        artifact = client.upload(data)
        board.command("flash.write", {"chip": "esp32p4", "return_console": False,
                                      "images": [{"address": 0x100000, "artifact": artifact["id"], "sha256": artifact["sha256"]}]}, allow_destructive=True)
        client.delete_artifact(artifact["id"])
    run, reports = run_pc(tmp_path, url, "test_pc_cycle.py", cycle_session(tmp_path, data=data), "--bench-allow-destructive")
    assert reports["PC-4"]["status"] == "BLOCKED" and "already identical" in " ".join(reports["PC-4"]["incidents"])
    assert "flash.write" not in commands(tmp_path)

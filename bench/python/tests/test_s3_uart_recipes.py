"""Software checks of the S3 / external UART recipes (issue #2, requirement 1). SIMULATION ONLY: a RAM simulator that DECLARES
itself an esp32s3; no real S3, no UART bridge. Proves the decisions of the recipe (BLOCKED / PASS / FAIL), never anything about a card."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_c3_read_only import base, run_recipe, sim   # noqa: E402,F401  (sim is a fixture)

S3 = dict(chip="esp32s3", flash_size=4 * 1024 * 1024, transport="external_uart", accept_unknown_transport=True,
          partition_table_offset=None, consent={"scenarios": ["S3R-RESET"]})
RECIPE = "test_s3_uart_reset.py"


def run(sim, tmp_path, **change):
    url, client = sim(chip="esp32s3")
    return run_recipe(tmp_path, url, RECIPE, **{**S3, **change}), client


def test_en_reset_with_rom_banner_passes(sim, tmp_path):
    (proc, reports), client = run(sim, tmp_path)
    assert proc.returncode == 0 and reports["S3R-1"]["status"] == "PASS", proc.stdout[-1200:]
    assert any("'ESP-ROM:esp32s3' observed after the reset, same proof window" in e for e in reports["S3R-1"]["evidence"])
    assert client.device("board")["reservation"] is None


def test_the_recipe_sends_exactly_one_reset_and_nothing_else_destructive(sim, tmp_path):
    (proc, reports), client = run(sim, tmp_path)
    requests = [json.loads(l) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for l in path.read_text().splitlines()]
    commands = [r for r in requests if r["method"] == "POST" and r["path"] == "/api/v1/operations"]
    assert len(commands) == 1
    ops = [json.loads(l)["command"] for path in (tmp_path / "evidence").glob("*/board-*/operations.jsonl") for l in path.read_text().splitlines()]
    assert set(ops) == {"reset"}


def test_missing_banner_is_a_fail_never_a_pass(sim, tmp_path):
    (proc, reports), _ = run(sim, tmp_path, readonly={"reset": {"marker": "NEVER-EMITTED", "marker_timeout_s": 2}})
    assert reports["S3R-1"]["status"] == "FAIL" and "no 'NEVER-EMITTED'" in " ".join(reports["S3R-1"]["incidents"])


@pytest.mark.parametrize("change,why", [
    ({"consent": {"scenarios": []}}, "S3R-RESET consent"),
    ({"chip": "esp32c3"}, "targets an esp32s3"),
    ({"transport": "usb_serial_jtag"}, "targets the external_uart transport"),
])
def test_wrong_session_is_blocked_before_any_evidence_or_command(sim, tmp_path, change, why):
    (proc, reports), _ = run(sim, tmp_path, **change)
    assert not reports and "BLOCKED" in proc.stdout and why in proc.stdout, proc.stdout[-800:]
    assert not list((tmp_path / "evidence").glob("*/requests.jsonl"))


def test_without_the_reset_opt_in_nothing_runs(sim, tmp_path, monkeypatch):
    import subprocess
    url, _ = sim(chip="esp32s3")
    (tmp_path / "evidence").mkdir()
    # same call as run_recipe but WITHOUT --qual-allow-reset
    import os
    session = {**base(), "evidence_root": str(tmp_path / "evidence"), **S3}
    (tmp_path / "session.json").write_text(json.dumps(session))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": "pytest-client-test-token", "BENCH_URL": url, "BENCH_QUAL_SESSION": str(tmp_path / "session.json")}
    bench = Path(__file__).resolve().parents[2]
    proc = subprocess.run([sys.executable, "-m", "pytest", f"recipes/qualification_campaign/{RECIPE}", "--bench", "--qual-allow-dirty",
                           "-q", "-rs", "-p", "no:cacheprovider"], cwd=bench, env=env, capture_output=True, text=True, timeout=120)
    assert "--qual-allow-reset not given" in proc.stdout and not list((tmp_path / "evidence").glob("*/report.json"))

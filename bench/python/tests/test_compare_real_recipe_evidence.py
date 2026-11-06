"""The comparator v2 reads the evidence the REAL recipes write (run against the RAM simulator): the raw-data correlation checks must be
satisfied by what the client and the recipes actually record, not only by hand-made fixtures. SIMULATION ONLY: this proves the formats
fit, never anything about a card. The overall verdict is not asserted (the run is on a dirty tree: --qual-allow-dirty)."""
import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qualification_compare as qc                                      # noqa: E402
import test_issue2_recipes as issue2                                    # noqa: E402
import test_s3_uart_recipes as s3                                       # noqa: E402
from test_issue2_recipes import sim as sim_issue2                       # noqa: E402,F401  (fixture)
from test_c3_read_only import sim as sim_s3                            # noqa: E402,F401  (fixture)

V2 = json.loads((BENCH / "tools" / "qualification_criteria_v2.json").read_text())
OWN = {"S3R-1": ["submitted_args_reset", "uart_match_correlated"],
       "S3R-2": ["submitted_args_boot.download", "hold_ms_bounded", "hold_ms_matches_request", "uart_match_correlated"],
       "S3R-3": ["submitted_args_flash.read", "console_return_correlated"],
       "R4": ["r4_images_match_manifest", "r4_readbacks_equal_submitted_images", "r4_restoration_matches_backups", "r4_control_windows_unchanged"]}


def judge(tmp_path, scenario):
    dirs = sorted((tmp_path / "evidence").glob(f"{scenario}-*"))
    assert len(dirs) == 1, dirs
    row = qc.assess(qc.Run(dirs[0]), V2)
    outcomes = {c["name"]: c["outcome"] for c in row["checks"]}
    assert all(outcomes.get(name) == "OK" for name in OWN[scenario]), {n: outcomes.get(n) for n in OWN[scenario]} | {"reasons": row["reasons"]}
    return row


def test_s3r1_real_evidence_satisfies_the_raw_correlation(sim_s3, tmp_path):
    url, _ = sim_s3(chip="esp32s3")
    proc, reports = s3.run_recipe(tmp_path, url, s3.RECIPE, **s3.S3)
    assert reports["S3R-1"]["status"] == "PASS", proc.stdout[-800:]
    judge(tmp_path, "S3R-1")


def test_s3r2_real_evidence_satisfies_the_raw_correlation(sim_s3, tmp_path):
    proc, reports, _ = s3.run_boot(sim_s3, tmp_path, script=[(4, 15)])
    assert reports["S3R-2"]["status"] == "PASS", proc.stdout[-800:]
    judge(tmp_path, "S3R-2")


def test_s3r3_real_evidence_satisfies_the_raw_correlation(sim_issue2, tmp_path):
    url, _ = sim_issue2(env={"SIM_CONSOLE_EVENT": "1"})
    proc, reports = issue2.run(tmp_path, url, "test_s3_uart_console_return.py", issue2.READ)
    assert reports["S3R-3"]["status"] == "PASS", proc.stdout[-800:]
    judge(tmp_path, "S3R-3")


def test_r4_real_evidence_satisfies_the_raw_correlation(sim_issue2, tmp_path):
    url, _ = issue2.prepared(sim_issue2)
    proc, reports = issue2.run(tmp_path, url, "test_r4_multi_image.py", issue2.R4, "--bench-allow-destructive")
    assert reports["R4"]["status"] == "PASS", proc.stdout[-1500:]
    judge(tmp_path, "R4")

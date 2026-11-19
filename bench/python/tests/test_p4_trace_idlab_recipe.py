"""The IDLab recipe driver: plan-only by default, authorization gate, evidence assembly. Never reaches a bench."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2] / "diagnostics" / "p4_usb_trace"
spec = importlib.util.spec_from_file_location("idlab_recipe", ROOT / "idlab_recipe.py")
recipe = importlib.util.module_from_spec(spec); sys.modules["idlab_recipe"] = recipe; spec.loader.exec_module(recipe)
sys.path.insert(0, str(ROOT)); import analyze_trace  # noqa: E402


def test_default_run_only_prints_the_plan_and_imports_no_client(tmp_path, capsys):
    stub = tmp_path / "stub.json"; stub.write_bytes(b"{}")
    assert recipe.main(["--stub", str(stub), "--board", "esp32-local"]) == 0
    out = capsys.readouterr()
    plan = json.loads(out.out)
    assert [s["label"] for s in plan["steps"]] == ["A1", "D", "T", "A2"] and "PLAN ONLY" in out.err
    assert plan["region"] == {"address": 0x8000, "size": 1048576, "packet_size": 4096} and "NOR write or erase" in plan["never"]


def test_execution_refused_without_matching_authorization(tmp_path):
    stub = tmp_path / "stub.json"; stub.write_bytes(b"{}")
    sha = hashlib.sha256(b"{}").hexdigest()
    with pytest.raises(SystemExit):
        recipe.main(["--stub", str(stub), "--board", "b", "--execute"])
    good = {"operator_statement": "mots de l'opérateur", "board": "b", "scenario": recipe.SCENARIO, "stub_sha256": sha}
    for change in ({"board": "other"}, {"stub_sha256": "0" * 64}, {"scenario": "x"}, {"operator_statement": " "}):
        auth = tmp_path / "a.json"; auth.write_text(json.dumps({**good, **change}))
        with pytest.raises(SystemExit):
            recipe.check_authorization(auth, "b", sha)
    auth = tmp_path / "ok.json"; auth.write_text(json.dumps(good))
    assert recipe.check_authorization(auth, "b", sha)["board"] == "b"


def test_read_arguments_use_the_documented_readers_and_the_fixed_region():
    assert recipe.read_args("A1")["reader"] == "reference" and recipe.read_args("D")["reader"] == "md5-only" and "reader" not in recipe.read_args("A2")
    assert all(recipe.read_args(x)["address"] == 0x8000 and recipe.read_args(x)["size"] == 1048576 for x in ("A1", "D", "A2"))


def test_evidence_assembled_from_operation_results_is_accepted_by_the_analyzer():
    t = {"host_duration_ms": 23000, "md5": "ab" * 16, "data_sha256": "c" * 64,
         "trace": {"flash_cycles": "40000000", "md5_cycles": "30000000", "send_cycles": "3000000000", "ack_cycles": "1500000000",
                   "fill_cycles": "2500000000", "fill_calls": 4096, "packets": 256, "ack_count": 256, "bytes": 1048576}}
    std = lambda ms: {"duration_ms": ms, "md5": "ab" * 16, "sha256": "c" * 64}
    ev = recipe.evidence_from_results({"A1": std(23100), "D": {"duration_ms": 2175, "md5": "ab" * 16}, "T": t, "A2": std(23200)})
    assert ev["schema"] == analyze_trace.SCHEMA and [r["label"] for r in ev["runs"]] == ["A1", "D", "T", "A2"]
    report = analyze_trace.analyze(ev, 200_000_000)
    assert report["verdict"] in {"ROM_CDC_FIFO_FILL", "SLIP_ENCODING_AND_SEND", "ACK_WAIT_AND_HOST_SYNC", "NO_SINGLE_DOMINANT_PHASE", "INDETERMINATE", "INVALID", "UNSTABLE", "CLOCK_ASSUMPTION_SUSPECT"}

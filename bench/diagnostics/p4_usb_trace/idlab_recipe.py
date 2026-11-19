#!/usr/bin/env python3
"""IDLab-driven recipe A1 -> D -> T -> A2 for the P4 USB OTG read profile. NOT RUN: by default it only PRINTS the plan.

Every hardware step goes through the official client -> IDLab API -> browser agent. Nothing here talks to a port.
The operator gestures (ROM download mode before T, local recovery and reset before A2, local grant) stay manual:
the script waits for a typed confirmation and never performs them.

    python3 idlab_recipe.py --stub stub.json --board esp32-local                      # prints the plan, touches nothing
    python3 idlab_recipe.py --stub stub.json --board esp32-local --execute \\
        --authorization authorization.json --evidence-dir NEW_EMPTY_DIR               # ONLY after the operator's explicit authorization

authorization.json (written by the operator, verbatim words, never reused): {"operator_statement": "...", "board": "...",
"scenario": "diagnostic.p4_usb_trace", "stub_sha256": "<64 hex>"} - all four must match this run.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REGION = {"address": 0x8000, "size": 1048576, "packet_size": 4096}
CPU_HZ = 200_000_000
SCENARIO = "diagnostic.p4_usb_trace"
STEPS = [
    ("A1", "flash.read", "standard reader (reference), standard stub"),
    ("D", "flash.read", "reader md5-only (stub-side MD5, no transfer)"),
    ("T", SCENARIO, "OPERATOR: put the P4 in ROM download mode (BOOT + reset) with manual download enabled; RAM load + traced read"),
    ("A2", "flash.read", "OPERATOR: local recovery, own reset, re-identify; then the standard reader again"),
]


def plan(board, stub_sha256):
    return {"board": board, "scenario": SCENARIO, "region": REGION, "cpu_hz_assumed": CPU_HZ, "stub_sha256": stub_sha256,
            "steps": [{"label": l, "command": c, "note": n} for l, c, n in STEPS],
            "never": ["NOR write or erase", "automatic reset", "retry of an uncertain operation", "stock-stub fallback", "reuse of a consent"]}


def read_args(label):
    args = {"address": REGION["address"], "size": REGION["size"], "return_console": False}
    if label == "A1":
        args["reader"] = "reference"
    elif label == "D":
        args["reader"] = "md5-only"
    return args


def check_authorization(path, board, stub_sha256):
    auth = json.loads(Path(path).read_text())
    expected = {"board": board, "scenario": SCENARIO, "stub_sha256": stub_sha256}
    if not isinstance(auth, dict) or not str(auth.get("operator_statement", "")).strip() or any(auth.get(k) != v for k, v in expected.items()):
        raise SystemExit("authorization does not match this board, scenario and stub SHA-256: nothing was run")
    return auth


def evidence_from_results(results):
    """results: {'A1': {...}, 'D': {...}, 'T': {...}, 'A2': {...}} -> idlab-p4-usb-trace-evidence/1 (raw cycles kept)."""
    def reader(label):
        r = results[label]
        run = {"label": label, "duration_ms": r["duration_ms"], "md5": r["md5"]}
        if "sha256" in r:
            run["sha256"] = r["sha256"]
        return run
    t = results["T"]
    trace = {k: t["trace"][k] for k in ("flash_cycles", "md5_cycles", "send_cycles", "ack_cycles", "fill_cycles", "fill_calls", "packets", "ack_count", "bytes")}
    return {"schema": "idlab-p4-usb-trace-evidence/1", "region": dict(REGION),
            "runs": [reader("A1"), reader("D"),
                     {"label": "T", "duration_ms": t["host_duration_ms"], "md5": t["md5"], "sha256": t["data_sha256"], "trace": trace},
                     reader("A2")]}


def standard_read(board, label):
    import time
    start = time.monotonic()
    result = board.command("flash.read", read_args(label), timeout=600)
    elapsed = round((time.monotonic() - start) * 1000)
    if label == "D":
        return {"duration_ms": result["transfer"]["elapsed_ms"], "md5": result["md5"], "operation_wall_ms": elapsed}
    data = board.client.download(result["artifact"])
    return {"duration_ms": result["transfer"]["elapsed_ms"], "md5": hashlib.md5(data).hexdigest(), "sha256": hashlib.sha256(data).hexdigest(),
            "artifact": result["artifact"]["id"], "operation_wall_ms": elapsed}


def execute(client, board_id, stub_path, evidence_dir, confirm=input):   # pragma: no cover - needs the real bench and the operator
    from iobewi_bench import BenchClient  # noqa: F401  (the official client, imported only when really executing)
    stub = Path(stub_path).read_bytes()
    out = Path(evidence_dir)
    out.mkdir(parents=True, exist_ok=False)          # a NEW directory per run
    results = {}
    with client.reserve(board_id, ttl=900, allow_destructive=True, output_dir=out) as board:
        results["A1"] = standard_read(board, "A1")
        results["D"] = standard_read(board, "D")
        confirm("Operator: board in ROM download mode with manual download enabled, local grant given? Type 'rom-mode-ok': ")
        artifact = client.upload(stub_path)
        sha = hashlib.sha256(stub).hexdigest()
        consent = {"scenario": SCENARIO, "device": board_id, "stub_sha256": sha, "address": REGION["address"], "size": REGION["size"],
                   "ram_code_execution": True, "session_effects_acknowledged": True, "rom_reset_may_be_required": True}
        args = {"chip": "esp32p4", "stub_artifact": artifact["id"], "stub_sha256": sha, "address": REGION["address"], "size": REGION["size"],
                "packet_size": REGION["packet_size"], "cpu_hz": CPU_HZ, "consent": consent}
        results["T"] = board.command(SCENARIO, args, timeout=300, allow_destructive=True)   # any uncertain outcome raises: STOP, no retry
        confirm("Operator: perform local recovery and your own reset, then confirm. Type 'recovered': ")
        board.retire_after_recovery()
    with client.reserve(board_id, ttl=900, output_dir=out) as board:
        results["A2"] = standard_read(board, "A2")
    evidence = evidence_from_results(results)
    (out / "evidence.json").write_text(json.dumps(evidence, indent=2))
    return evidence


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stub", required=True); p.add_argument("--board", required=True)
    p.add_argument("--execute", action="store_true"); p.add_argument("--authorization"); p.add_argument("--evidence-dir")
    a = p.parse_args(argv)
    sha = hashlib.sha256(Path(a.stub).read_bytes()).hexdigest()
    print(json.dumps(plan(a.board, sha), indent=2))
    if not a.execute:
        print("PLAN ONLY: nothing was run.", file=sys.stderr)
        return 0
    if not (a.authorization and a.evidence_dir):
        raise SystemExit("--execute needs --authorization and a NEW --evidence-dir")
    check_authorization(a.authorization, a.board, sha)
    from iobewi_bench import BenchClient
    execute(BenchClient(os.environ["BENCH_URL"], os.environ["BENCH_TOKEN"]), a.board, a.stub, a.evidence_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

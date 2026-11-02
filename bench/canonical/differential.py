#!/usr/bin/env python3
"""Reproducible host differential for the canonical-length rule of the application's upload path.

Pins the sources (commit and blob identifiers), the inputs (complete SHA-256) and the output; rebuilds the driver `upload-check/`
against a clean export of #105 and compares its table with `differential.expected.txt`. Needs git, cargo (offline) and the frozen
artefacts. Writes only under --work. Exit status 0 only if every check passes.

  differential.py --neg DIR_WITH_N1_N2 [--repo rust-mcu-libs] [--web web/p91] [--work DIR]
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
fails = []


def check(ok, what, detail=""):
    print(("PASS" if ok else "FAIL"), what, ("— " + str(detail)) if detail else "")
    if not ok:
        fails.append(what)
    return ok


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout


def sha(b):
    return hashlib.sha256(b).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(HERE.parent.parent / "rust-mcu-libs"))
    ap.add_argument("--web", default=str(HERE.parent.parent / "web/p91"))
    ap.add_argument("--neg", required=True)
    ap.add_argument("--work", default=os.environ.get("BENCH_DIFF_WORK", "/tmp/bench-canonical-diff"))
    a = ap.parse_args()
    repo, work = Path(a.repo), Path(a.work)
    expected = json.loads((HERE / "differential.pins.json").read_text())
    for short, full in expected["commits"].items():
        check(git(repo, "rev-parse", short).strip() == full, f"commit {short} is {full[:12]}…")
    for path, per in expected["blobs"].items():
        for short, blob in per.items():
            check(git(repo, "rev-parse", f"{short}:{path}").strip() == blob, f"{path} at {short}", blob[:12])
    # what #105 changes in the upload: exactly the pinned lines, nothing else in ota.rs
    d = git(repo, "diff", "-U0", "cc3a8a1", "9ba9533", "--", "examples/firmware/ota-system/src/ota.rs")
    lines = [l for l in d.splitlines() if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---"))]
    check(sha("\n".join(lines).encode()) == expected["upload_hunk_sha256"], "#105 changes in ota.rs: exactly the pinned lines", f"{len(lines)} lines")
    check(any(".is_err()" in l and l.startswith("-") for l in lines) and any("canonical_size(partition.offset) != Some(params.total)" in l for l in lines),
          "the old rule is `.is_err()`, the new one adds `canonical_size(partition.offset) != Some(params.total)`")
    paths = {}
    for name, (ref, want) in expected["inputs"].items():
        kind, fn = ref.split(":")
        f = Path(a.web if kind == "web" else a.neg) / fn
        ok = f.exists() and sha(f.read_bytes()) == want
        check(ok, f"input {name}: {fn} complete SHA-256", want if ok else "missing or different")
        paths[name] = f
    if fails:
        sys.exit(1)
    src = work / "src"
    if not (src / ".exported").exists():
        src.mkdir(parents=True, exist_ok=True)
        tar = subprocess.run(["git", "-C", str(repo), "archive", "9ba9533"], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(src)], input=tar, check=True)
        (src / ".exported").write_text("9ba9533\n")
    drv = work / "driver"
    (drv / "src").mkdir(parents=True, exist_ok=True)
    (drv / "Cargo.toml").write_text((HERE / "upload-check/Cargo.toml.template").read_text().replace("@P105@", str(src)))
    (drv / "src/main.rs").write_text((HERE / "upload-check/src/main.rs").read_text())
    env = {**os.environ, "CARGO_TARGET_DIR": str(work / "target")}
    for tool in ("rustc", "cargo"):
        print("  ", subprocess.run([tool, "--version"], capture_output=True, text=True).stdout.strip())
    r = subprocess.run(["cargo", "run", "-q", "--offline", "--", *[str(paths[n]) for n in ("B2 image", "B2 proof", "N1 image", "N1 proof", "N2 image", "N2 proof", "A'' slot (restore file)")]],
                       cwd=drv, env=env, capture_output=True, text=True)
    check(r.returncode == 0, "driver built and ran against the clean export of 9ba9533", r.stderr[-300:])
    got = [l.rstrip() for l in r.stdout.splitlines()]
    want = [l.rstrip() for l in (HERE / "differential.expected.txt").read_text().splitlines()]
    check(got == want, "table identical to differential.expected.txt")
    if got != want:
        print("\n".join(got))
    print("\nRESULT:", "PASS" if not fails else "FAIL (%d)" % len(fails))
    sys.exit(1 if fails else 0)


main()

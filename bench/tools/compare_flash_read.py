#!/usr/bin/env python3
"""Read-only ESP32-P4 A/B/C comparison: Python esptool leg against IDLab evidence.

Hardware execution is gated by --execute and an explicit serial port. No write
or erase commands are supported. Results go to a private local directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
import time

EXPECTED_A_SHA = "a5bd08050eb97eb0e3c223deeead165e22c3cdfd59dcea691fbaa56980c50bf0"
OFFSET = 0x10000
SIZE = 0x100000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def compare_evidence(b_sha: str, c_sha: str | None) -> dict:
    matches_a = b_sha.lower() == EXPECTED_A_SHA
    matches_c = c_sha is None or c_sha.lower() == EXPECTED_A_SHA
    return {
        "reference_idlab_a": EXPECTED_A_SHA,
        "esptool_python_b": b_sha.lower(),
        "idlab_c": c_sha.lower() if c_sha is not None else None,
        "b_matches_a": matches_a,
        "c_matches_a": matches_c if c_sha is not None else None,
        "integrity_verdict": ("PASS" if matches_a and matches_c and c_sha is not None
                              else "FAIL" if not matches_a or (c_sha is not None and not matches_c)
                              else "PENDING_IDLAB_C"),
    }


def run_b(port: str, outdir: Path, python: str, c_sha: str | None) -> dict:
    if not port.strip() or port.strip().startswith("-"):
        raise ValueError("An explicit valid --port is mandatory")
    if outdir.exists() and any(outdir.iterdir()):
        raise FileExistsError("Output directory must be empty to preserve existing evidence")
    outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / "esp32p4-python-B-0x10000-1MiB.bin"
    command = [python, "-m", "esptool", "--chip", "esp32p4", "--port", port,
               "--baud", "115200", "--after", "no-reset",
               "read-flash", hex(OFFSET), hex(SIZE), str(output)]
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()
    # The only device operation in this script is the explicit read-flash command.
    proc = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False)
    elapsed = time.perf_counter() - t0
    log = outdir / "esptool-B.log"
    log.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr, encoding="utf-8")
    # esptool's own duration excludes setup/stub upload and file output; unlike
    # wall time it is directly comparable to IDLab's flash transfer duration.
    summary = re.search(
        r"Read (\d+) bytes from (0x[0-9a-fA-F]+) in (\d+(?:\.\d+)?) seconds",
        proc.stdout + "\n" + proc.stderr,
    )
    read_seconds = float(summary.group(3)) if summary and int(summary.group(1)) == SIZE and int(summary.group(2), 16) == OFFSET else None
    report = {
        "esptool_read_elapsed_s": read_seconds,
        "esptool_read_bytes_per_second": round(SIZE / read_seconds) if read_seconds and read_seconds > 0 else None,
        "tool": "esptool-python",
        "command": "read-flash",
        "chip": "esp32p4",
        "address": OFFSET,
        "bytes_requested": SIZE,
        "started_utc": started,
        "wall_elapsed_ms": round(elapsed * 1000),
        "exit_code": proc.returncode,
        "port": port,
        "output": str(output),
        "log": str(log),
    }
    if proc.returncode == 0 and output.is_file() and output.stat().st_size == SIZE:
        digest = sha256_file(output)
        report.update({"sha256": digest, "bytes_read": output.stat().st_size,
                       "end_to_end_bytes_per_second": round(SIZE / elapsed) if elapsed else None,
                       "integrity": compare_evidence(digest, c_sha)})
    else:
        report["status"] = "INCOMPLETE"
        report["bytes_read"] = output.stat().st_size if output.exists() else 0
    (outdir / "report-B.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Explicit Windows COM port or Linux /dev/tty*")
    parser.add_argument("--outdir", type=Path, help="Existing empty or new evidence directory")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter with esptool installed")
    parser.add_argument("--idlab-c-sha", help="SHA-256 of a new post-Python IDLab 1 MiB read at 0x10000")
    parser.add_argument("--execute", action="store_true",
                        help="Explicit consent to one read-only hardware command")
    args = parser.parse_args(argv)
    if args.idlab_c_sha is not None and (len(args.idlab_c_sha) != 64 or
                                        any(c not in "0123456789abcdefABCDEF" for c in args.idlab_c_sha)):
        parser.error("--idlab-c-sha must be a 64-character hexadecimal SHA-256")
    if not args.execute:
        print("DRY RUN: no port opened and no flash read performed.")
        print("Would run: python -m esptool --chip esp32p4 --port PORT --baud 115200 "
              "--after no-reset read-flash 0x10000 0x100000 OUTPUT.bin")
        print("To run after releasing Web Serial: --execute --port COMx --outdir PRIVATE_EMPTY_DIR")
        return 0
    if not args.port or args.outdir is None:
        parser.error("--execute requires --port and --outdir")
    try:
        report = run_b(args.port, args.outdir, args.python, args.idlab_c_sha)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2))
    return 0 if report["exit_code"] == 0 and report.get("integrity", {}).get("b_matches_a") else 1


if __name__ == "__main__":
    raise SystemExit(main())

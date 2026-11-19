#!/usr/bin/env python3
"""Reproducible offline build of the standard and the IDLab-instrumented ESP32-P4 stub.

    python3 build_stub.py --upstream /path/to/esp-flasher-stub --toolchain-bin /path/to/riscv32-esp-elf/bin \\
        --out /path/to/NEW/evidence/dir

Never opens a serial port, never touches a device. Requirements, all checked before building:
- the upstream checkout is exactly tag v1.2.2 with the pinned esp-stub-lib, trees clean;
- riscv32-esp-elf GCC 15.1.0 (crosstool-NG esp-15.1.0_20250607), the toolchain the upstream
  `tools/setup_toolchains.sh` pins;
- CMake >= 3.28 (the upstream minimum), Ninja, and pyelftools in the Python running this script.

Writes into the NEW output directory (it must not exist): both source copies, both builds with
their logs, `build-manifest.json` (versions, commits, SHA-256 of patch, ELF and JSON) and
`static-qualification.json`.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


apply_trace, qualify_stub = _load("apply_trace"), _load("qualify_stub")
TARGET_CHIP = "esp32p4"
GCC_EXPECTED = "esp-15.1.0_20250607"
CMAKE_MINIMUM = (3, 28)


def run(command, cwd=None, env=None):
    try:
        return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit(f"required tool not found: {command[0]}")


def tool_versions(toolchain_bin, env):
    gcc = run([str(Path(toolchain_bin) / "riscv32-esp-elf-gcc"), "--version"], env=env)
    if gcc.returncode != 0:
        raise SystemExit(f"riscv32-esp-elf-gcc not usable in {toolchain_bin}")
    first = gcc.stdout.splitlines()[0]
    if GCC_EXPECTED not in first:
        raise SystemExit(f"toolchain is '{first}', expected crosstool-NG {GCC_EXPECTED}")
    cmake = run(["cmake", "--version"], env=env).stdout.splitlines()[0]
    found = tuple(int(x) for x in re.search(r"(\d+)\.(\d+)\.(\d+)", cmake).groups()[:2])
    if found < CMAKE_MINIMUM:
        raise SystemExit(f"{cmake}: upstream needs CMake >= {'.'.join(map(str, CMAKE_MINIMUM))}")
    ninja = run(["ninja", "--version"], env=env)
    if ninja.returncode != 0:
        raise SystemExit("ninja not found")
    try:
        import elftools
    except ImportError:
        raise SystemExit("pyelftools is required (pip install pyelftools)")
    return {"gcc": first, "cmake": cmake, "ninja": ninja.stdout.strip(), "pyelftools": elftools.__version__,
            "python": sys.version.split()[0]}


def build_one(source, label, env):
    build = source / "build-p4"
    for step, command in (("configure", ["cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
                                         f"-DTARGET_CHIP={TARGET_CHIP}"]),
                          ("build", ["cmake", "--build", str(build)])):
        done = run(command, env=env)
        (source.parent / f"{label}-{step}.log").write_text(done.stdout + done.stderr)
        if done.returncode != 0:
            raise SystemExit(f"{label} {step} failed, see {source.parent / (label + '-' + step + '.log')}")
    log = (source.parent / f"{label}-build.log").read_text()
    warnings = len(re.findall(r"\bwarning\b|\berror\b", log))
    return {"elf": build / "src" / f"stub-{TARGET_CHIP}.elf", "json": build / f"{TARGET_CHIP}.json",
            "warnings_or_errors_in_log": warnings}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--toolchain-bin", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="new directory, must not exist")
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"{args.out} exists: a build never reuses or overwrites a previous evidence directory")
    env = dict(os.environ, PATH=f"{args.toolchain_bin}{os.pathsep}{os.environ['PATH']}")
    versions = tool_versions(args.toolchain_bin, env)
    try:
        apply_trace.verify_upstream(args.upstream)
    except ValueError as error:
        raise SystemExit(f"refused: {error}")
    args.out.mkdir(parents=True)
    stock_src, traced_src = args.out / "src-stock", args.out / "src-traced"
    shutil.copytree(args.upstream, stock_src)
    shutil.copytree(args.upstream, traced_src)
    apply_trace.patch(traced_src, verify=True)
    stock, traced = build_one(stock_src, "stock", env), build_one(traced_src, "traced", env)
    qualification = qualify_stub.qualify(stock["elf"], stock["json"], traced["elf"], traced["json"],
                                         args.toolchain_bin)
    (args.out / "static-qualification.json").write_text(json.dumps(qualification, indent=2, default=str))
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": TARGET_CHIP,
        "upstream_tag": apply_trace.UPSTREAM_TAG, "upstream_commit": apply_trace.UPSTREAM_COMMIT,
        "esp_stub_lib_commit": apply_trace.SUBMODULE_COMMIT,
        "tools": versions,
        "patch_script_sha256": qualify_stub.sha256((HERE / "apply_trace.py").read_bytes()),
        "stock": {"elf_sha256": qualification["images"]["stock_elf_sha256"],
                  "json_sha256": qualification["images"]["stock_json_sha256"],
                  "warnings_or_errors_in_log": stock["warnings_or_errors_in_log"]},
        "traced": {"elf_sha256": qualification["images"]["traced_elf_sha256"],
                   "json_sha256": qualification["images"]["traced_json_sha256"],
                   "warnings_or_errors_in_log": traced["warnings_or_errors_in_log"]},
        "qualified_statically": qualification["qualified_statically"],
        "note": "Compiled and statically checked. Not validated on hardware.",
    }
    (args.out / "build-manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 0 if qualification["qualified_statically"] else 1


if __name__ == "__main__":
    sys.exit(main())

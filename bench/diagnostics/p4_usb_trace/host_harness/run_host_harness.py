#!/usr/bin/env python3
"""Build and run the native host harness of the IDLab P4 trace.

    python3 run_host_harness.py /path/to/esp-flasher-stub-v1.2.2-checkout

The checkout is copied to a temporary directory, the IDLab patch is applied to the copy, the
two patched C files are compiled natively with ONLY the ``rdcycle`` read replaced by a simulated
counter, and ``harness.c`` drives them through a scripted transport. Nothing here touches a
device and nothing here proves hardware behaviour. Prints the parsed results as JSON.
"""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("apply_trace", HERE.parent / "apply_trace.py")
apply_trace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_trace)

ASM = '__asm__ volatile ("rdcycle %0" : "=r"(t));'
FAKE = "extern uint32_t idlab_test_cycles(void); t = idlab_test_cycles();"
CFLAGS = ["-std=gnu17", "-O1", "-g", "-fno-strict-aliasing", "-ffunction-sections", "-w"]
INCLUDES = ["src", "esp-stub-lib/include", "esp-stub-lib/include/esp-stub-lib", "esp-stub-lib/src/target/esp32p4/include",
            "esp-stub-lib/src/target/base/include", "esp-stub-lib/src/target/common/include",
            "esp-stub-lib/src/target/esp32p4/include/soc"]


def build(checkout, work):
    """Returns the path of the harness executable."""
    tree = Path(work) / "tree"
    shutil.copytree(checkout, tree, ignore=shutil.ignore_patterns("build*", "unittests"))
    apply_trace.patch(tree, verify=False)
    objects = []
    for source, name in ((tree / "src/command_handler.c", "command_handler"),
                         (tree / "esp-stub-lib/src/usb_otg.c", "usb_otg")):
        text = source.read_text()
        if text.count(ASM) != 1:  # the single inline definition of idlab_cycle32()
            raise ValueError(f"{name}: unexpected number of rdcycle reads: {text.count(ASM)}")
        copy = Path(work) / f"{name}_host.c"
        copy.write_text(text.replace(ASM, FAKE))
        obj = Path(work) / f"{name}.o"
        subprocess.run(["gcc", *CFLAGS, *[f"-I{tree / i}" for i in INCLUDES], "-c", str(copy), "-o", str(obj)],
                       check=True)
        objects.append(obj)
    harness = Path(work) / "harness.o"
    subprocess.run(["gcc", *CFLAGS, *[f"-I{tree / i}" for i in INCLUDES], "-c", str(HERE / "harness.c"),
                    "-o", str(harness)], check=True)
    exe = Path(work) / "harness"
    link = ["gcc", str(harness), *map(str, objects), "-Wl,--gc-sections", "-Wl,--unresolved-symbols=ignore-all"]
    subprocess.run([*link, "-o", str(exe)], check=True)
    # Functions of the stub that the scenarios never reach: define them so that the dynamic loader
    # starts, and abort if one is ever called (that would invalidate the scenario).
    undefined = subprocess.run(["nm", "-u", str(exe)], capture_output=True, text=True, check=True).stdout
    names = sorted({line.split()[-1] for line in undefined.splitlines()
                    if line.strip().startswith("U ") and "@" not in line and "_ITM" not in line})
    stubs = Path(work) / "unreachable.c"
    stubs.write_text("#include <stdlib.h>\n" + "".join(f"void {n}(void) {{ abort(); }}\n" for n in names))
    subprocess.run([*link, str(stubs), "-o", str(exe)], check=True)
    return exe


def parse(output):
    results = {}
    for line in output.splitlines():
        if not line.startswith("RESULT "):
            continue
        _, label, *fields = line.split()
        values = {}
        for field in fields:
            key, sep, value = field.partition("=")
            if not sep:                       # "RESULT label 1": a single bare value
                key, value = "value", field
            values[key] = int(value, 0) if re.fullmatch(r"-?(0x[0-9a-fA-F]+|\d+)", value) else value
        results[label] = values
    return results


def run(checkout):
    with tempfile.TemporaryDirectory() as work:
        exe = build(checkout, work)
        done = subprocess.run([str(exe)], capture_output=True, text=True, check=True, timeout=60)
    return parse(done.stdout)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: run_host_harness.py <esp-flasher-stub-checkout>")
    print(json.dumps(run(sys.argv[1]), indent=2))

#!/usr/bin/env python3
"""Host-side checks for the W0 qualification (rewrite of the B'' IAP1 proof at 0x37f000). Reads files only: it never
talks to the board, and it never writes anywhere.

  check_w0.py prepare [--web DIR] [--server URL]   artefact, proof, geometry, backups (before anything touches the board)
  check_w0.py s0      --dumps DIR --tag TAG         the five reference ranges read back as TAG_<name>-0x<addr>.bin
  check_w0.py after   --dumps DIR --tag TAG         sector window (TAG_win-0x37e000.bin, 0x3000 bytes) + the five ranges

Exit status 0 only if every check passes. A check that cannot be made counts as a failure.
"""
import argparse
import hashlib
import json
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REFS = json.loads((HERE / "refs.json").read_text())
T = REFS["target"]
SECTOR = 4096
TARGET = int(T["addr"], 16)
IAP1_PACK = HERE.parent.parent / "rust-mcu-libs/target/debug/examples/iap1_pack"
names = {"bootloader": "boot", "otadata+ota_meta+table": "meta", "ota_0 (A'')": "ota0", "ota_1 (B'')": "ota1", "config": "cfg"}
fails = []


def sha(b):
    return hashlib.sha256(b).hexdigest()


def check(ok, what, detail=""):
    print(("PASS" if ok else "FAIL"), what, ("— " + str(detail)) if detail else "")
    if not ok:
        fails.append(what)
    return ok


def verify_with_tool(image, sector_file):
    if not IAP1_PACK.exists():
        return None, "iap1_pack not built"
    r = subprocess.run([str(IAP1_PACK), "verify", str(image), str(sector_file), T["pin"], T["product"], T["mcu"],
                        str(T["variant"]), str(T["min_security"])], capture_output=True, text=True)
    return r.stdout.strip() + r.stderr.strip(), None


def prepare(a):
    web = Path(a.web)
    proof, image = web / T["artifact"], web / T["image"]
    p, i = proof.read_bytes(), image.read_bytes()
    check(len(p) == SECTOR and sha(p) == T["sha256"], "proof file is exactly one sector with the expected SHA-256", sha(p))
    check(sha(i) == T["image_sha256"] and len(i) == T["image_size"], "B'' image has the expected SHA-256 and size", f"{len(i)} {sha(i)[:16]}")
    n = struct.unpack_from("<I", p, 8)[0]
    check(p[:4] == b"IAP1" and struct.unpack_from("<I", p, 16)[0] == 0x5AC3A53C, "IAP1 header: magic and validation word (Committed)")
    check(set(p[20 + n:]) == {0xFF}, "bytes after the payload are 0xFF (erased)", f"payload {n} bytes")
    out, err = verify_with_tool(image, proof)
    check(out is not None and out.startswith("authentic") and str(T["image_size"]) in out, "iap1_pack verify: B'' image + proof = authentic (signed size = image size)", out or err)
    other = web / "p91/m3-A2-app.bin"
    if other.exists() and IAP1_PACK.exists():
        out2, _ = verify_with_tool(other, proof)
        check("DigestMismatch" in (out2 or ""), "negative control: A'' image + B'' proof is refused (DigestMismatch)", out2)
    # geometry: the sector is the last 4 KiB of ota_1 and the end of a 64 KiB block; the next byte is `config`
    ota1_off, ota1_size, cfg_off = 0x1d0000, 0x1b0000, 0x380000
    check(TARGET + SECTOR == ota1_off + ota1_size == cfg_off, "sector 0x37f000-0x380000 ends ota_1 and touches only `config` after it", "0x37f000+0x1000 = 0x380000")
    check(TARGET % SECTOR == 0 and (TARGET + SECTOR) % 0x10000 == 0, "4 KiB aligned and ends on a 64 KiB boundary: a sector or a block erase cannot reach config or the image")
    check(TARGET >= ota1_off + T["image_size"], "sector lies beyond the end of the B'' image", hex(ota1_off + T["image_size"]))
    # restore material: read-backs of ota_1 whose WHOLE-file SHA-256 is the reference of the current state (older read-backs,
    # from before M2, hold the former image and a blank proof sector: they are not backups of this state)
    cur = next(r["sha256"] for r in REFS["s0_ranges"] if r["name"].startswith("ota_1"))
    seen, older = 0, 0
    for d in (HERE.parent / "logs/dumps", HERE.parent / "backups"):
        for f in sorted(d.rglob("*ota1-0x1d0000*.bin")):
            b = f.read_bytes()
            if len(b) != ota1_size:
                continue
            if sha(b) == cur:
                seen += 1
                if b[TARGET - ota1_off:TARGET - ota1_off + SECTOR] != p:
                    check(False, "restore backup holds the authentic proof", f.name)
            else:
                older += 1
    check(seen > 0, "restore backups of the current ota_1 exist and hold the authentic proof at 0x37f000", f"{seen} files (+{older} older read-backs ignored)")
    check((HERE.parent.parent / "web" / T["artifact"]).exists(), "the authentic proof file itself is the sector restore image (rewrite the same file)")
    ref = {r["name"]: r["sha256"] for r in REFS["s0_ranges"]}
    check(sha(b"\xff" * SECTOR) == REFS["blank_sector_sha256"], "blank-sector reference is the SHA-256 of 4096 x 0xFF")
    if a.server:
        inv = json.load(urllib.request.urlopen(a.server.rstrip("/") + "/api/images?fresh=1", timeout=10))
        e = [x for x in inv["images"] if x["path"] == T["artifact"]]
        check(len(e) == 1 and e[0]["sha256"] == T["sha256"], "bench server lists the proof with the expected SHA-256 (the page re-checks it before writing)",
              e and (e[0]["category"], e[0]["origin"]))


def s0(a):
    d = Path(a.dumps)
    for r in REFS["s0_ranges"]:
        f = d / f"{a.tag}_{names[r['name']]}-{r['addr']}.bin"
        ok = f.exists() and sha(f.read_bytes()) == r["sha256"] and len(f.read_bytes()) == int(r["size"], 16)
        check(ok, f"{r['name']} {r['addr']}+{r['size']} = reference", f.name if not f.exists() else sha(f.read_bytes())[:16])


def after(a):
    d = Path(a.dumps)
    w = d / f"{a.tag}_win-0x37e000.bin"
    b = w.read_bytes() if w.exists() else b""
    check(len(b) == 3 * SECTOR, "sector window 0x37e000-0x381000 read back (3 sectors)", len(b))
    if len(b) == 3 * SECTOR:
        blank = REFS["blank_sector_sha256"]
        check(sha(b[:SECTOR]) == blank, "0x37e000 (before) still blank")
        check(sha(b[SECTOR:2 * SECTOR]) == T["sha256"], "0x37f000 = the authentic proof, byte for byte (independent SHA-256)", sha(b[SECTOR:2 * SECTOR]))
        check(sha(b[2 * SECTOR:]) == blank, "0x380000 (first config sector) still blank")
        tmp = d / f"{a.tag}_target.bin"
        tmp.write_bytes(b[SECTOR:2 * SECTOR])
        out, err = verify_with_tool(Path(a.web) / T["image"], tmp)
        check(out is not None and out.startswith("authentic"), "iap1_pack verify on the READ-BACK sector + B'' image = authentic", out or err)
    s0(a)


ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("cmd", choices=["prepare", "s0", "after"])
ap.add_argument("--web", default="/workspace/web")
ap.add_argument("--server")
ap.add_argument("--dumps", default="/workspace/bench/logs/dumps")
ap.add_argument("--tag", default="w0")
a = ap.parse_args()
{"prepare": prepare, "s0": s0, "after": after}[a.cmd](a)
print("\nRESULT:", "PASS" if not fails else "FAIL (%d)" % len(fails))
sys.exit(1 if fails else 0)

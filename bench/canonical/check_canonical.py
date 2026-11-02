#!/usr/bin/env python3
"""Host-side checks for the canonical-length qualification of the application's upload path. Reads files only.

  check_canonical.py prepare --frozen DIR                       frozen artefacts: manifest, authenticity, signed sizes
  check_canonical.py show    --dumps D --tag T                  otadata and ota_meta of one checkpoint (decoded with the P5-C codecs)
  check_canonical.py capture --file F --stage S [--from TXT]   S in refused | accepted | activate-c | reboot-c (UART of one step)
  check_canonical.py refused-state --before T0 --after T1 --image FILE
                                                                state left by one refused upload into ota_1 (bytes written, erase span, blank proof, metadata)
  check_canonical.py plan-restore --current T --reference R --out DIR
                                                                the 4 KiB sectors that differ from the reference, as ROM write commands (idempotent)
  check_canonical.py diff    --dumps D --before T0 --after T1 --allow ZONE[,ZONE...]
                                                                every zone that differs must be allowed; nothing else may change

Zones (from the partition table): boot 0x0-0xA000, gap 0xA000-0x10000, table 0x10000-0x11000, otadata 0x11000-0x13000,
ota_meta 0x13000-0x15000, ota0_image 0x20000-0x1CF000, ota0_proof 0x1CF000-0x1D0000, ota1_image 0x1D0000-0x37F000,
ota1_proof 0x37F000-0x380000, config 0x380000-0x400000. A checkpoint is the six read-backs TAG_{boot-0x0,gap-0xa000,meta-0x10000,
ota0-0x20000,ota1-0x1d0000,cfg-0x380000}.bin. Exit status 0 only if every check passes.
"""
import argparse
import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_p5c", HERE.parent / "p5c/check_p5c.py")
P5C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P5C)
PIN = P5C.R["pin"]
IAP1_PACK = HERE.parent.parent / "rust-mcu-libs/target/debug/examples/iap1_pack"
ZONES = {"boot": (0x0, 0xA000), "gap": (0xA000, 0x10000), "table": (0x10000, 0x11000), "otadata": (0x11000, 0x13000), "ota_meta": (0x13000, 0x15000),
         "ota0_image": (0x20000, 0x1CF000), "ota0_proof": (0x1CF000, 0x1D0000), "ota1_image": (0x1D0000, 0x37F000), "ota1_proof": (0x37F000, 0x380000),
         "config": (0x380000, 0x400000)}
FILES = [("boot-0x0", 0x0), ("gap-0xa000", 0xA000), ("meta-0x10000", 0x10000), ("ota0-0x20000", 0x20000), ("ota1-0x1d0000", 0x1D0000), ("cfg-0x380000", 0x380000)]
fails = P5C.fails
check = P5C.check


def picture(d, tag):
    """address -> bytes for the ranges of one checkpoint."""
    out = {}
    for suffix, base in FILES:
        f = Path(d) / f"{tag}_{suffix}.bin"
        if not f.exists():
            return None
        out[base] = f.read_bytes()
    return out


def zone_bytes(pic, zone):
    lo, hi = ZONES[zone]
    for base, b in pic.items():
        if base <= lo and hi <= base + len(b):
            return b[lo - base:hi - base]
    raise KeyError(zone)


def verify(image, proof):
    if not IAP1_PACK.exists():
        return None
    r = subprocess.run([str(IAP1_PACK), "verify", str(image), str(proof), PIN, "iobewi-ota-sys", "esp32s3", "0", "0"], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def prepare(a):
    d = Path(a.frozen)
    m = d.parent / "MANIFEST.sha256"
    lines = [l.split() for l in m.read_text().splitlines()] if m.exists() else []
    check(bool(lines) and all(hashlib.sha256((d / n).read_bytes()).hexdigest() == h for h, n in lines), f"frozen manifest verifies ({len(lines)} files, complete SHA-256)")
    for name in ("app", "N1", "N2"):
        img, prf = d / f"{name}.bin", d / f"{name}.iap1"
        out = verify(img, prf)
        want = img.stat().st_size
        check(out is not None and out.startswith("authentic") and f"Some({want})" in out, f"{name}: valid RSA-PSS proof over exactly its {want} bytes (digest of the signed bytes)", out)
    b2 = HERE.parent.parent / "web/p91/m2-B2-app.bin"
    if b2.exists():
        b = b2.read_bytes()
        check((d / "N1.bin").read_bytes() == b + b"\x00" * 16, "N1 = B'' image + 16 zero bytes (canonical 1050112 < total 1050128)")
        check((d / "N2.bin").read_bytes() == b[:1048576], "N2 = first 1 MiB of the B'' image (canonical 1050112 > total 1048576)")


def show(a):
    pic = picture(a.dumps, a.tag)
    check(pic is not None, f"checkpoint {a.tag} present")
    if pic:
        meta = pic[0x10000]
        print("   otadata:", P5C.describe(P5C.otadata_of(meta)), "| ota_meta:", P5C.meta_of(meta)[0])


def diff(a):
    before, after = picture(a.dumps, a.before), picture(a.dumps, a.after)
    check(before is not None and after is not None, f"checkpoints {a.before} and {a.after} present")
    if before is None or after is None:
        return
    allow = set(filter(None, a.allow.split(",")))
    check(allow <= set(ZONES), "allowed zones are known", sorted(allow - set(ZONES)))
    for z in ZONES:
        try:
            was, now = zone_bytes(before, z), zone_bytes(after, z)
        except KeyError:
            continue
        same = was == now
        if z in allow:
            print(f"     {z:<11} {'unchanged (allowed to change)' if same else 'changed (allowed)'}")
        else:
            check(same, f"{z} unchanged", "" if same else f"{hashlib.sha256(was).hexdigest()[:12]} -> {hashlib.sha256(now).hexdigest()[:12]}")


def capture(a):
    text = Path(a.file).read_text(errors="replace")
    if a.after:
        i = text.find(a.after)
        check(i >= 0, f"start marker present: {a.after!r}")
        text = text[i:] if i >= 0 else ""
    import re
    lines = [re.sub(r"^\d\d:\d\d:\d\d\.\d+ \[uart\] ", "", l) for l in text.splitlines()]
    has = lambda p: any(re.search(p, l) for l in lines)
    def ordered(patterns, label):
        pos = -1
        for p in patterns:
            j = next((k for k in range(pos + 1, len(lines)) if re.search(p, lines[k])), None)
            if not check(j is not None, f"{label}: {p}"):
                return
            pos = j
    fatal = r"Signature Check Failed|panic|EXCEPTION|HALT|Guru|abort\(\)|rst:0x|boot authentication|refused"
    if a.stage in ("refused", "accepted"):
        check(not has(fatal), "no signature failure, panic, exception, reset or boot line (the board did not restart)")
        check(has(r"ota: writing .* \(\d+ bytes\) to ota_[01]"), "the upload was authorized and written (signature accepted before the first erase)")
        if a.stage == "refused":
            check(not has(r"flash digest and image verified; publishing"), "never reached publication")
        else:
            check(has(r"flash digest and image verified; publishing"), "reached publication")
    elif a.stage == "activate-c":
        ordered([r"ota: activating .*; reboot requested", "boot authentication: VERIFIED", "boot: slots checked", r"plan slot=0x00000000 seq=0x00000003",
                 r"otadata write sector=0x00000000 seq=0x00000003 state=0x00000001", "boot: image ok", "boot: jump", r"boot: running ota_0 status PendingVerify",
                 "boot: confirmed", "OTA ready"], "activation of the #105 application (New -> PendingVerify -> confirmed)")
        check(not has(r"Signature Check Failed|panic|EXCEPTION|HALT|refused|status Rollback"), "no refusal, panic, HALT or rollback")
    elif a.stage == "reboot-c":
        ordered(["boot authentication: VERIFIED", "boot: slots checked", r"plan slot=0x00000000 seq=0x00000003", "boot: image ok", "boot: jump",
                 r"boot: running ota_0 status Stable", "boot: stable", "OTA ready"], "#105 application after the persistence reboot")
        check(not has(r"otadata write|boot: confirmed|refused|panic|HALT"), "no write, no new confirmation, no refusal")


def refused_state(a):
    """After an upload refused at the readback stage: the slot holds exactly the bytes that were received, up to the end of the 64 KiB
    erase span; the proof sector is erased and never rewritten; nothing outside ota_1 changed (ota_meta included)."""
    before, after = picture(a.dumps, a.before), picture(a.dumps, a.after)
    check(before is not None and after is not None, f"checkpoints {a.before} and {a.after} present")
    if before is None or after is None:
        return
    img = Path(a.image).read_bytes()
    slot = zone_bytes(after, "ota1_image")
    span = -(-len(img) // 65536) * 65536
    check(slot[:len(img)] == img, f"ota_1 holds exactly the {len(img)} received bytes (the digest of the flash is the digest that was sent)", hashlib.sha256(slot[:len(img)]).hexdigest()[:16])
    check(set(slot[len(img):span]) <= {255}, f"erase span: ota_1 {len(img)}..{span:#x} is erased (0xFF)")
    check(set(zone_bytes(after, "ota1_proof")) == {255}, "ota_1 proof sector is erased and was not rewritten (the upload is not publishable, not activatable)")
    for z in ZONES:
        if z in ("ota1_image", "ota1_proof"):
            continue
        check(zone_bytes(before, z) == zone_bytes(after, z), f"{z} unchanged")


RESTORABLE = ("ota0_image", "ota0_proof", "ota1_image", "ota1_proof", "otadata", "ota_meta")


def plan_restore(a):
    """Restoration driven by the difference: a sector is rewritten only if it differs from the reference, and only inside the zones a
    campaign step may have touched. Anything else that differs (bootloader, table, config, gap) is an anomaly: nothing is planned."""
    cur, ref = picture(a.dumps, a.current), picture(a.dumps, a.reference)
    check(cur is not None and ref is not None, f"checkpoints {a.current} and {a.reference} present")
    if cur is None or ref is None:
        return
    runs = []
    for z, (lo, hi) in ZONES.items():
        was, now = zone_bytes(ref, z), zone_bytes(cur, z)
        if was == now:
            continue
        if z not in RESTORABLE:
            check(False, f"{z} differs from the reference and is not restorable by this plan: stop, report", f"{hashlib.sha256(was).hexdigest()[:12]} != {hashlib.sha256(now).hexdigest()[:12]}")
            continue
        for off in range(0, hi - lo, 4096):
            if was[off:off + 4096] != now[off:off + 4096]:
                at = lo + off
                if runs and runs[-1][0] + runs[-1][1] == at:
                    runs[-1][1] += 4096
                else:
                    runs.append([at, 4096])
    if fails:
        return
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"   {len(runs)} run(s), {sum(n for _, n in runs)} bytes to write (everything else already conforms)")
    for at, n in runs:
        data = bytes(next(b[at - base:at - base + n] for base, b in ref.items() if base <= at and at + n <= base + len(b)))
        f = out / f"p6r-{a.reference}-0x{at:x}-{n:x}.bin"
        f.write_bytes(data)
        print(f"   write {f.name}@0x{at:x}   ({n} bytes, sha256 {hashlib.sha256(data).hexdigest()})")
    check(True, "restoration plan computed from the read-back (re-run it after any interruption: it only shrinks)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["prepare", "show", "diff", "capture", "plan-restore", "refused-state"])
    ap.add_argument("--image")
    ap.add_argument("--current")
    ap.add_argument("--reference")
    ap.add_argument("--out")
    ap.add_argument("--file")
    ap.add_argument("--stage", choices=["refused", "accepted", "activate-c", "reboot-c"])
    ap.add_argument("--from", dest="after")
    ap.add_argument("--frozen", default="")
    ap.add_argument("--dumps", default="/workspace/bench/logs/dumps")
    ap.add_argument("--tag")
    ap.add_argument("--before")
    ap.add_argument("--after")
    ap.add_argument("--allow", default="")
    a = ap.parse_args(argv)
    {"prepare": prepare, "show": show, "diff": diff, "capture": capture, "plan-restore": plan_restore, "refused-state": refused_state}[a.cmd](a)
    print("\nRESULT:", "PASS" if not fails else "FAIL (%d)" % len(fails))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

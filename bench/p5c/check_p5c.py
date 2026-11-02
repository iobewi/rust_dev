#!/usr/bin/env python3
"""Host-side checks for the P5-C qualification (full OTA under VerifiedBoot). Reads files only: no board, no network, no writes.

  check_p5c.py prepare [--web DIR]                      artefacts, restore material, codec self-test against the real S0 bytes
  check_p5c.py flash   --dumps DIR --tag T --state S    S in s0 | s1 | c1 | r1 : the six read-back ranges of one checkpoint
  check_p5c.py capture --file F --stage G [--from TXT]  G in s1 | a1b1 | r1 : the UART capture of one step (from the first line containing TXT)

The ranges are read back by the ROM loader as TAG_{boot-0x0,gap-0xa000,meta-0x10000,ota0-0x20000,ota1-0x1d0000,cfg-0x380000}.bin.
Exit status 0 only if every check passes; a check that cannot be made counts as a failure.
"""
import argparse
import hashlib
import json
import re
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
R = json.loads((HERE / "refs.json").read_text())
BASE = {r["name"]: r for r in json.loads((HERE.parent / "w0/refs_after_p5b.json").read_text())["ranges"]}
FILES = {"bootloader": "boot-0x0", "gap": "gap-0xa000", "otadata+ota_meta+table": "meta-0x10000", "ota_0": "ota0-0x20000", "ota_1": "ota1-0x1d0000", "config": "cfg-0x380000"}
fails = []


def sha(b):
    return hashlib.sha256(b).hexdigest()


def check(ok, what, detail=""):
    print(("PASS" if ok else "FAIL"), what, ("— " + str(detail)) if detail else "")
    if not ok:
        fails.append(what)
    return ok


# ---- otadata: the codec of common/firmware/boot (32-byte entry at the start of each 4 KiB sector)
NEW, PENDING, VALID, INVALID, ABORTED = 0, 1, 2, 3, 4
STATE = {NEW: "New", PENDING: "PendingVerify", VALID: "Valid", INVALID: "Invalid", ABORTED: "Aborted"}
COMMIT = 0x5AC3A53C


def crc_le(init, data):          # esp_rom_crc32_le(init, ..) == zlib.crc32(data, init)
    return zlib.crc32(data, init)


def encode_entry(seq, state):
    raw = bytearray(b"\xff" * 32)
    raw[0:4] = struct.pack("<I", seq)
    raw[4:8] = b"BOOT"
    raw[8:12] = struct.pack("<I", 2)
    raw[12:16] = struct.pack("<I", crc_le(0xFFFFFFFF, struct.pack("<II", seq, state) + b"BOOT" + struct.pack("<I", 2)))
    raw[20:24] = struct.pack("<I", COMMIT)
    raw[24:28] = struct.pack("<I", state)
    raw[28:32] = struct.pack("<I", crc_le(0xFFFFFFFF, struct.pack("<I", seq)))
    return bytes(raw)


def decode_entry(sector):
    raw = bytes(sector[:32])
    if raw == b"\xff" * 32:
        return "blank"
    seq, state = struct.unpack_from("<I", raw, 0)[0], struct.unpack_from("<I", raw, 24)[0]
    if state in STATE and seq not in (0, 0xFFFFFFFF) and raw == encode_entry(seq, state):
        return (seq, state)
    return "corrupt"


def otadata_of(meta):             # meta = 0x10000..0x15000
    return [meta[0x1000:0x2000], meta[0x2000:0x3000]]


def describe(sectors):
    out = []
    for s in sectors:
        d = decode_entry(s)
        out.append(d if isinstance(d, str) else f"seq{d[0]} {STATE[d[1]]}")
    return out


# ---- ota_meta: two OTSF frames carrying an OTM1 record
def decode_frame(b):
    if b[:4] == b"\xff" * 4 and set(b[:16]) == {255}:
        return "blank"
    if b[:4] != b"OTSF" or b[4] != 1:
        return "invalid"
    n, seq = struct.unpack_from("<HI", b, 6)
    body = 12 + ((n + 3) & ~3)
    if n > 512 or struct.unpack_from("<I", b, body)[0] != zlib.crc32(b[:body]):
        return "invalid"
    return (seq, b[12:12 + n])


def decode_otm1(p):
    if p[:4] != b"OTM1":
        return None
    stage, size = p[4], struct.unpack_from("<I", p, 5)[0]
    lens = struct.unpack_from("<5H", p, 9)
    cur, f = 19, []
    for n in lens:
        f.append(p[cur:cur + n].decode())
        cur += n
    return {"stage": {0: "none", 1: "written", 2: "activating"}[stage], "size": size, "slot": f[0], "digest": f[1], "deployment_id": f[2],
            "active_digest": f[3], "active_deployment_id": f[4]} if cur == len(p) else None


def meta_of(meta):                # ota_meta = 0x13000..0x15000 = meta[0x3000:0x5000]
    frames = [decode_frame(meta[0x3000:0x4000]), decode_frame(meta[0x4000:0x5000])]
    valid = [(i, f) for i, f in enumerate(frames) if isinstance(f, tuple)]
    if not valid:
        return None, frames
    i, (seq, payload) = max(valid, key=lambda x: x[1][0])      # no wrap in practice (sequences are tiny)
    return {"copy": i, "seq": seq, **(decode_otm1(payload) or {"stage": "?"})}, frames


def load(d, tag):
    out = {}
    for name, suffix in FILES.items():
        f = Path(d) / f"{tag}_{suffix}.bin"
        out[name] = f.read_bytes() if f.exists() else None
    return out


def prepare(a):
    web = Path(a.web)
    for key in ("bootloader_verified", "bootloader_unsigned_restore"):
        e = R[key]
        f = web / e["file"]
        b = f.read_bytes() if f.exists() else b""
        check(len(b) == e["size"] and sha(b) == e["sha256"], f"{key}: {e['file']} has the expected size and complete SHA-256", sha(b))
    # codec self-test on bytes that really were on the board
    d = Path(a.dumps)
    f = d / f"{a.s0}_meta-0x10000.bin"
    meta = f.read_bytes() if f.exists() else b""
    check(len(meta) == 0x5000 and sha(meta[0x1000:0x3000]) == R["s0_otadata_sha256"] and sha(meta[0x3000:0x5000]) == R["s0_ota_meta_sha256"],
          "S0 backup (TAG_meta-0x10000.bin): otadata and ota_meta have the expected SHA-256")
    if len(meta) == 0x5000:
        o = otadata_of(meta)
        check(o[0] == encode_entry(1, VALID) + b"\xff" * (4096 - 32) and set(o[1]) == {255}, "codec self-test: S0 otadata sector 0 == encode(seq 1, Valid) byte for byte, sector 1 blank", describe(o))
        m, _ = meta_of(meta)
        check(m and m["stage"] == "written" and m["slot"] == "ota_1" and m["deployment_id"] == R["deployment_id"] and m["digest"] == R["staged_digest"] and m["size"] == 1050112,
              "S0 ota_meta: staged written, ota_1, 1050112 bytes, expected digest and deployment id", m)
        check(m and m["active_digest"] == "" and m["active_deployment_id"] == "", "S0 ota_meta: no active deployment recorded yet")
    # restore material, carved from the S0 read-back and verified against the references
    for tag, suffix in (("boot", "boot-0x0"), ("ota0", "ota0-0x20000"), ("ota1", "ota1-0x1d0000")):
        f = d / f"{a.s0}_{suffix}.bin"
        ref = BASE[{"boot": "bootloader", "ota0": "ota_0", "ota1": "ota_1"}[tag]]["sha256"]
        check(f.exists() and sha(f.read_bytes()) == ref, f"S0 backup of {tag} matches refs_after_p5b", f.name)
    ver = (web / R["bootloader_verified"]["file"]).read_bytes() if (web / R["bootloader_verified"]["file"]).exists() else b""
    check(len(ver) <= 0x9000, "verified bootloader fits below 0x9000 (the erase of W1 cannot reach 0x9000-0xA000)", len(ver))


def expected_bootloader_after_w1(web, s0_boot):
    ver = (Path(web) / R["bootloader_verified"]["file"]).read_bytes()
    return ver + b"\xff" * (0x9000 - len(ver)) + s0_boot[0x9000:]


def flash(a):
    got = load(a.dumps, a.tag)
    for name, b in got.items():
        check(b is not None, f"read-back present: {name}")
    if any(b is None for b in got.values()):
        return
    s0b = Path(a.dumps) / f"{a.s0}_boot-0x0.bin"
    exp_boot = BASE["bootloader"]["sha256"] if a.state == "s0" else sha(expected_bootloader_after_w1(a.web, s0b.read_bytes()))
    check(sha(got["bootloader"]) == exp_boot, f"bootloader {'= unsigned reference' if a.state == 's0' else '= verified image + inert remainder (W1 form)'}", sha(got["bootloader"])[:16])
    for name in ("gap", "ota_0", "ota_1", "config"):
        check(sha(got[name]) == BASE[name]["sha256"], f"{name} = refs_after_p5b (B'' image and proof, A'' and its proof, config: never modified by P5-C)", sha(got[name])[:16])
    meta = got["otadata+ota_meta+table"]
    check(meta[:0x1000] == (Path(a.dumps) / f"{a.s0}_meta-0x10000.bin").read_bytes()[:0x1000], "partition table (0x10000-0x11000) unchanged")
    o, (m, frames) = otadata_of(meta), meta_of(meta)
    print("   otadata:", describe(o), "| ota_meta:", m)
    if a.state in ("s0", "s1"):
        check(sha(meta) == BASE["otadata+ota_meta+table"]["sha256"], "0x10000-0x15000 = refs_after_p5b (nothing written by the bootloader or by A'')")
    else:
        check(o[0] == encode_entry(1, VALID) + b"\xff" * 4064, "otadata sector 0: seq 1 Valid (A'' kept as the recoverable fallback), byte for byte")
        check(o[1] == encode_entry(2, VALID) + b"\xff" * 4064, "otadata sector 1: seq 2 Valid (B'' confirmed), byte for byte")
        s0m, _ = meta_of((Path(a.dumps) / f"{a.s0}_meta-0x10000.bin").read_bytes())
        check(m is not None and m["seq"] == s0m["seq"] + R["ota_meta_commits_expected"], f"ota_meta: newest frame sequence = S0 {s0m['seq']} + {R['ota_meta_commits_expected']} commits (Activating, promote, clear)", m and m["seq"])
        check(m is not None and m["stage"] == "none" and m["slot"] == "" and m["digest"] == "" and m["deployment_id"] == "", "ota_meta: staged cleared (transaction finished)")
        check(m is not None and m["active_digest"] == R["staged_digest"] and m["active_deployment_id"] == R["deployment_id"], "ota_meta: active deployment = B'' (digest and id)")
        other = frames[1 - m["copy"]] if m else None
        check(isinstance(other, tuple) and other[0] == m["seq"] - 1, "ota_meta: the other copy holds the previous record (two-copy protocol intact)", other if isinstance(other, str) else other[0])
        if a.state == "r1":
            ref = Path(a.dumps) / f"{a.ref}_meta-0x10000.bin"
            check(ref.exists() and ref.read_bytes() == meta, f"persistence: 0x10000-0x15000 identical to checkpoint {a.ref} (the extra reboot wrote nothing)")


def capture(a):
    text = Path(a.file).read_text(errors="replace")
    if a.after:
        i = text.find(a.after)
        check(i >= 0, f"start marker present: {a.after!r}")
        text = text[i:] if i >= 0 else ""
    lines = [re.sub(r"^\d\d:\d\d:\d\d\.\d+ \[uart\] ", "", l) for l in text.splitlines()]
    bad = [l for l in lines if re.search(r"Signature Check Failed|panic|EXCEPTION|HALT|refused|Guru|abort\(\)|rst:0x(7|8|b|c)\b", l, re.I)]
    check(not bad, "no refusal, panic, exception, HALT, watchdog reset or signature failure", bad[:3])

    def ordered(patterns, label):
        pos = -1
        for p in patterns:
            j = next((k for k in range(pos + 1, len(lines)) if re.search(p, lines[k])), None)
            if not check(j is not None, f"{label}: {p}"):
                return
            pos = j

    def absent(p, label):
        check(not any(re.search(p, l) for l in lines), label)

    boot = ["boot authentication: VERIFIED", "boot: slots checked", None, "boot: image ok", "boot: jump"]
    if a.stage == "s1":
        ordered([boot[0], boot[1], r"plan slot=0x00000000 seq=0x00000001", boot[3], boot[4], r"boot: running ota_0 status Stable", r"boot: stable", "OTA ready"], "A'' under VerifiedBoot")
        absent(r"otadata write", "the bootloader wrote nothing to otadata")
    elif a.stage == "a1b1":
        ordered([r"ota: activating p5-b2-20261101; reboot requested", boot[0], boot[1], r"plan slot=0x00000001 seq=0x00000002",
                 r"otadata write sector=0x00000001 seq=0x00000002 state=0x00000001", boot[3], boot[4],
                 r"boot: running ota_1 status PendingVerify", r"boot: confirmed", "OTA ready"], "activation, then B'' (New -> PendingVerify -> confirmed)")
        absent(r"otadata write sector=0x0000000[01] seq=0x00000001", "A'' entry untouched")
        absent(r"boot: running ota_0", "A'' did not run after the activation")
        absent(r"status Rollback|storage self-check failed|metadata completion failed|recovery failed", "no rollback, degraded or failed completion")
    elif a.stage == "r1":
        ordered([boot[0], boot[1], r"plan slot=0x00000001 seq=0x00000002", boot[3], boot[4], r"boot: running ota_1 status Stable", r"boot: stable", "OTA ready"], "B'' under VerifiedBoot after the persistence reboot")
        absent(r"otadata write", "the bootloader wrote nothing to otadata")
        absent(r"boot: confirmed|PendingVerify", "no new confirmation: B'' was already Valid")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["prepare", "flash", "capture"])
    ap.add_argument("--web", default="/workspace/web")
    ap.add_argument("--dumps", default="/workspace/bench/logs/dumps")
    ap.add_argument("--tag")
    ap.add_argument("--s0", default="p5bs0", help="tag of the S0 read-back of this campaign (default: the P5-B/P5-A one)")
    ap.add_argument("--state", choices=["s0", "s1", "c1", "r1"])
    ap.add_argument("--ref", default="p5c_c1", help="r1: tag of the c1 read-back")
    ap.add_argument("--file")
    ap.add_argument("--stage", choices=["s1", "a1b1", "r1"])
    ap.add_argument("--from", dest="after")
    a = ap.parse_args(argv)
    {"prepare": prepare, "flash": flash, "capture": capture}[a.cmd](a)
    print("\nRESULT:", "PASS" if not fails else "FAIL (%d)" % len(fails))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

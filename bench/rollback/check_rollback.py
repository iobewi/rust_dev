#!/usr/bin/env python3
"""Host-side checks for the rollback qualification (an authentic image that never confirms, then a reset). Reads files only.

  check_rollback.py prepare --frozen DIR            frozen artefacts: manifest, authenticity, canonical size, the trace string in D and not in C
  check_rollback.py capture --file F --stage S [--from TXT]
                                                    S in d-start | rollback | b-stable : UART of the activation, of D without confirmation,
                                                    of the reset and of the fall back on B'' (timing of the reset measured)
  check_rollback.py state --dumps D --before T0 --after T1 --image F --proof F
                                                    flash after the rollback: D left in ota_0 (authentic proof), otadata [seq 3 Aborted | seq 2 Valid]
                                                    with the B'' entry untouched, ota_meta cleaned (three frames more, staged empty, B'' still active)
Everything else (single-step uploads, restoration) uses check_canonical.py (accepted-state, diff, plan-restore).
Exit status 0 only if every check passes.
"""
import argparse
import hashlib
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_canonical", HERE.parent / "canonical/check_canonical.py")
CC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CC)
P5C, check = CC.P5C, CC.check
TRACE = b"rollback test image: self-check hangs on purpose"
DEADLINE_S = (14.5, 16.5)      # PENDING_VERIFY_TIMEOUT_MS = 15 000 + the 200 ms settling delay; hardware precedent: 15.2-15.3 s


def sha(b):
    return hashlib.sha256(b).hexdigest()


def prepare(a):
    d = Path(a.frozen)
    m = d.parent / "MANIFEST.sha256"
    lines = [l.split() for l in m.read_text().splitlines()] if m.exists() else []
    check(bool(lines) and all(sha((d / n).read_bytes()) == h for h, n in lines), f"frozen manifest verifies ({len(lines)} files, complete SHA-256)")
    img, prf = d / "D.bin", d / "D.iap1"
    out = CC.verify(img, prf)
    want = img.stat().st_size
    check(out is not None and out.startswith("authentic") and f"Some({want})" in out, f"D: valid RSA-PSS proof over exactly its {want} bytes", out)
    data = img.read_bytes()
    check(TRACE in data, "D contains the explicit trace string (rollback test image)")
    for other in ("C105.bin", "B2.bin"):
        f = d / other
        if f.exists():
            check(TRACE not in f.read_bytes(), f"{other} does not contain it (D is the only image that announces itself)")


def stamp(line):
    m = re.match(r"^(\d\d):(\d\d):(\d\d\.\d+) ", line)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def capture(a):
    text = Path(a.file).read_text(errors="replace")
    if a.after:
        i = text.find(a.after)
        check(i >= 0, f"start marker present: {a.after!r}")
        text = text[i:] if i >= 0 else ""
    raw = text.splitlines()
    lines = [re.sub(r"^\d\d:\d\d:\d\d\.\d+ \[uart\] ", "", l) for l in raw]

    def find(p, start=0):
        return next((k for k in range(start, len(lines)) if re.search(p, lines[k])), None)

    def ordered(patterns, label):
        pos, idx = -1, []
        for p in patterns:
            j = find(p, pos + 1)
            if not check(j is not None, f"{label}: {p}"):
                return None
            pos = j
            idx.append(j)
        return idx

    bad = r"Signature Check Failed|panic|EXCEPTION|HALT|Guru|abort\(\)|refused|rst:0x(7|8|b|c)\b"
    check(not any(re.search(bad, l, re.I) for l in lines), "no refusal, panic, exception, HALT, signature failure or watchdog reset (rst 7/8/b/c)")
    boot = ["boot authentication: VERIFIED", "boot: slots checked"]
    if a.stage in ("d-start", "rollback"):
        idx = ordered([r"ota: activating .*; reboot requested", *boot, r"plan slot=0x00000000 seq=0x00000003",
                       r"otadata write sector=0x00000000 seq=0x00000003 state=0x00000001", "boot: image ok", "boot: jump",
                       r"boot: running ota_0 status PendingVerify", TRACE.decode()], "D starts (New -> PendingVerify)")
        check(find(r"boot: confirmed") is None, "D never confirms")
        if a.stage == "rollback" and idx:
            pv = idx[-2]
            r = find(r"rst:0x3 \(RTC_SW_SYS_RST\)", pv + 1)
            if check(r is not None, "a software reset follows (rst:0x3), not a hardware watchdog"):
                dt = stamp(raw[r]) - stamp(raw[pv]) if stamp(raw[r]) is not None and stamp(raw[pv]) is not None else None
                check(dt is not None and DEADLINE_S[0] <= dt <= DEADLINE_S[1], f"reset {dt:.1f} s after PendingVerify (self-check deadline 15 s + 0.2 s settling)" if dt is not None else "timestamps readable")
                tail = lines[r:]
                pos = -1
                for p in [*boot, r"plan slot=0x00000001 seq=0x00000002", r"otadata write sector=0x00000000 seq=0x00000003 state=0x00000004",
                          "boot: image ok", "boot: jump", r"boot: running ota_1 status Stable", "boot: stable", "OTA ready"]:
                    j = next((k for k in range(pos + 1, len(tail)) if re.search(p, tail[k])), None)
                    if not check(j is not None, f"after the reset: {p}"):
                        break
                    pos = j
                check(not any(re.search(r"running ota_0|PendingVerify|boot: confirmed", l) for l in tail), "D is not started again; nothing is confirmed after the reset")
    elif a.stage == "b-stable":
        ordered([*boot, r"plan slot=0x00000001 seq=0x00000002", "boot: image ok", "boot: jump", r"boot: running ota_1 status Stable", "boot: stable", "OTA ready"], "B'' after the rollback")
        check(find(r"otadata write") is None, "the persistence boot writes nothing to otadata")
        check(find(r"running ota_0|PendingVerify|boot: confirmed") is None, "D is not started again")


def state(a):
    before, after = CC.picture(a.dumps, a.before), CC.picture(a.dumps, a.after)
    check(before is not None and after is not None, f"checkpoints {a.before} and {a.after} present")
    if before is None or after is None:
        return
    img, proof = Path(a.image).read_bytes(), Path(a.proof).read_bytes()
    slot = CC.zone_bytes(after, "ota0_image")
    span = -(-len(img) // 65536) * 65536
    check(slot[:len(img)] == img and set(slot[len(img):span]) <= {255}, "ota_0 holds D exactly (and a clean erase span): the image stays, only its entry is dead")
    check(CC.zone_bytes(after, "ota0_proof") == proof, "D's published proof is still in place (the rollback is not a refusal of D)")
    o = P5C.otadata_of(after[0x10000])
    od0 = P5C.otadata_of(before[0x10000])
    check(o[0] == P5C.encode_entry(3, P5C.ABORTED) + b"\xff" * 4064, "otadata sector 0: seq 3 Aborted, byte for byte")
    check(o[1] == od0[1] and o[1] == P5C.encode_entry(2, P5C.VALID) + b"\xff" * 4064, "otadata sector 1: B'' seq 2 Valid, byte for byte identical to before")
    m0, _ = P5C.meta_of(before[0x10000])
    m1, frames = P5C.meta_of(after[0x10000])
    ok = bool(m0 and m1)
    check(ok, "ota_meta: a valid newest frame before and after")
    if ok:
        check(m1["seq"] == m0["seq"] + 3, f"ota_meta: three frames more (Written D, Activating D, cleaned by B''): sequence {m0['seq']} -> {m1['seq']}")
        check(m1["stage"] == "none" and m1["slot"] == "" and m1["digest"] == "" and m1["deployment_id"] == "", "ota_meta: no transaction staged")
        check(m1["active_digest"] == m0["active_digest"] and m1["active_deployment_id"] == m0["active_deployment_id"], "ota_meta: B'' still the active deployment (D was never promoted)")
    for z in CC.ZONES:
        if z in ("ota0_image", "ota0_proof", "otadata", "ota_meta"):
            continue
        check(CC.zone_bytes(before, z) == CC.zone_bytes(after, z), f"{z} unchanged")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["prepare", "capture", "state"])
    ap.add_argument("--frozen", default="")
    ap.add_argument("--file")
    ap.add_argument("--stage", choices=["d-start", "rollback", "b-stable"])
    ap.add_argument("--from", dest="after")
    ap.add_argument("--dumps", default="/workspace/bench/logs/dumps")
    ap.add_argument("--before")
    ap.add_argument("--tag", dest="after_tag")
    ap.add_argument("--image")
    ap.add_argument("--proof")
    a = ap.parse_args(argv)
    if a.cmd == "state":
        a.after = a.after_tag
    {"prepare": prepare, "capture": capture, "state": state}[a.cmd](a)
    print("\nRESULT:", "PASS" if not CC.fails else "FAIL (%d)" % len(CC.fails))
    sys.exit(1 if CC.fails else 0)


if __name__ == "__main__":
    main()

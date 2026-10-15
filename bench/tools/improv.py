#!/usr/bin/env python3
"""Improv Serial client for the bench (needs full access; the page must be in console mode).

  improv.py state | info | scan | wifi SSID [PASSWORD]
  Credentials may also come from IMPROV_SSID / IMPROV_PASSWORD. They are sent as raw bytes
  through `benchctl tx` and are never written to the capture (the page logs only a byte count).

Frames: "IMPROV" 0x01 TYPE LEN DATA CHECKSUM ; TYPE 1 state, 2 error, 3 rpc command, 4 rpc result.
Replies are read back from the capture (`rx <hex>` lines, enabled here with `rxhex on`).
"""
import json, os, re, subprocess, sys, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
STATES = {1: "authorization required", 2: "authorized (ready to provision)", 3: "provisioning", 4: "provisioned"}
ERRORS = {0: "none", 1: "invalid rpc", 2: "unknown rpc", 3: "unable to connect", 4: "not authorized", 255: "unknown"}

def frame(ftype, data):
    f = b"IMPROV" + bytes([1, ftype, len(data)]) + data
    return f + bytes([sum(f) & 0xFF])

def rpc(cmd, strings=()):
    body = b"".join(bytes([len(s)]) + s for s in strings)
    return frame(3, bytes([cmd, len(body)]) + body)

def benchctl(*args):
    return subprocess.run([sys.executable, os.path.join(HERE, "benchctl.py"), *args], capture_output=True, text=True, timeout=120)

def capture_path():
    return json.load(urllib.request.urlopen("http://localhost:8080/api/info"))["capture"]

def parse(stream):
    out, i = [], 0
    while True:
        i = stream.find(b"IMPROV", i)
        if i < 0 or i + 9 > len(stream): break
        ftype, n = stream[i + 7], stream[i + 8]
        if i + 9 + n + 1 > len(stream): break
        f = stream[i:i + 9 + n + 1]
        ok = sum(f[:-1]) & 0xFF == f[-1]
        out.append((ftype, f[9:9 + n], ok)); i += len(f)
    return out

def describe(ftype, data, ok):
    if ftype == 1: return f"state: {STATES.get(data[0], data[0])}"
    if ftype == 2: return f"error: {ERRORS.get(data[0], data[0])}"
    if ftype == 4:
        cmd, n, rest, items = data[0], data[1], data[2:], []
        while rest:
            items.append(rest[1:1 + rest[0]].decode("utf-8", "replace")); rest = rest[1 + rest[0]:]
        return f"rpc result cmd={cmd}: {items}"
    return f"type {ftype}: {data.hex()}"

def exchange(payload, wait=6.0):
    benchctl("rxhex", "on")
    mark = "improv-%d" % int(time.time() * 1000)
    benchctl("mark", mark)
    r = benchctl("tx", payload.hex())
    if '"ok": true' not in r.stdout: sys.exit("tx failed: " + (r.stdout or r.stderr))
    time.sleep(wait)
    text = open(capture_path(), errors="replace").read()
    text = text[text.rindex("MARK " + mark):]
    raw = b"".join(bytes.fromhex(h) for h in re.findall(r"\[bench\] rx ([0-9a-f]+)", text))
    frames = parse(raw)
    for t, d, ok in frames: print(("" if ok else "BAD-CHECKSUM ") + describe(t, d, ok))
    if not frames: print("no Improv frame received (%d raw bytes)" % len(raw))
    return frames

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "state": exchange(rpc(0x02))
    elif cmd == "info": exchange(rpc(0x03))
    elif cmd == "scan": exchange(rpc(0x04), wait=12)
    elif cmd == "wifi":
        ssid = (sys.argv[2] if len(sys.argv) > 2 else os.environ["IMPROV_SSID"]).encode()
        pw = (sys.argv[3] if len(sys.argv) > 3 else os.environ.get("IMPROV_PASSWORD", "")).encode()
        exchange(rpc(0x01, [ssid, pw]), wait=float(os.environ.get("IMPROV_WAIT", "25")))
    else: sys.exit(__doc__)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Improv Serial client for Windows (pyserial). Provisions Wi-Fi on a StreamBeWI board over its native USB port.

  py improv_serial.py COM7 state
  py improv_serial.py COM7 info
  py improv_serial.py COM7 wifi [--env C:\\...\\bench\\.improv.env]     (SSID/password from the file, or IMPROV_SSID / IMPROV_PASSWORD)

Prints the board's states and errors only; the credentials are sent and never printed.
DTR and RTS are held low from before the port opens: on the ESP32-S3 USB-Serial-JTAG they drive reset and boot mode.
"""
import argparse, os, sys, time
import serial  # pip install pyserial

STATES = {1: "authorization required", 2: "authorized (ready to provision)", 3: "provisioning", 4: "provisioned"}
ERRORS = {0: "none", 1: "invalid rpc", 2: "unknown rpc", 3: "unable to connect", 4: "not authorized", 255: "unknown"}

def frame(ftype, data):
    f = b"IMPROV" + bytes([1, ftype, len(data)]) + data
    return f + bytes([sum(f) & 0xFF])

def rpc(cmd, strings=()):
    body = b"".join(bytes([len(s)]) + s for s in strings)
    return frame(3, bytes([cmd, len(body)]) + body)

def parse(stream):
    out, i = [], 0
    while True:
        i = stream.find(b"IMPROV", i)
        if i < 0 or i + 9 > len(stream): break
        t, n = stream[i + 7], stream[i + 8]
        if i + 10 + n > len(stream): break
        f = stream[i:i + 10 + n]
        out.append((t, f[9:9 + n], sum(f[:-1]) & 0xFF == f[-1])); i += len(f)
    return out

def describe(t, d, ok):
    pre = "" if ok else "BAD-CHECKSUM "
    if t == 1: return pre + "state: " + STATES.get(d[0], str(d[0]))
    if t == 2: return pre + "error: " + ERRORS.get(d[0], str(d[0]))
    if t == 4:
        items, rest = [], d[2:]
        while rest: items.append(rest[1:1 + rest[0]].decode("utf-8", "replace")); rest = rest[1 + rest[0]:]
        return pre + "rpc result cmd=%d: %s" % (d[0], items)
    return pre + "type %d" % t

def read_env(path):
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("port"); ap.add_argument("cmd", choices=["state", "info", "scan", "wifi"])
    ap.add_argument("--env"); ap.add_argument("--wait", type=float, default=30); a = ap.parse_args()
    ser = serial.Serial(); ser.port = a.port; ser.baudrate = 115200; ser.timeout = 0.2
    ser.dtr = False; ser.rts = False; ser.open(); ser.dtr = False; ser.rts = False
    time.sleep(0.3); ser.reset_input_buffer()
    if a.cmd == "state": payload, wait = rpc(0x02), 4
    elif a.cmd == "info": payload, wait = rpc(0x03), 4
    elif a.cmd == "scan": payload, wait = rpc(0x04), 12
    else:
        env = read_env(a.env) if a.env else {}
        ssid = env.get("IMPROV_SSID") or os.environ.get("IMPROV_SSID"); pw = env.get("IMPROV_PASSWORD", os.environ.get("IMPROV_PASSWORD", ""))
        if not ssid: sys.exit("no SSID: give --env FILE or set IMPROV_SSID")
        payload, wait = rpc(0x01, [ssid.encode(), pw.encode()]), a.wait
    ser.write(payload); ser.flush()
    raw, end = b"", time.time() + wait
    seen = 0
    while time.time() < end:
        raw += ser.read(256)
        for t, d, ok in parse(raw)[seen:]:
            print(describe(t, d, ok)); seen += 1
            if a.cmd == "wifi" and ((t == 1 and d[0] == 4) or t == 2 and d[0] != 0): end = time.time() + 1
    if not seen: print("no Improv frame received (%d bytes of other data)" % len(raw))
    ser.close()

if __name__ == "__main__":
    main()

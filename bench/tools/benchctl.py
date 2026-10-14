#!/usr/bin/env python3
"""Drive the bench page from the container and wait for the outcome.

The page (index.html) holds the serial port and executes the commands it pulls from the
server. Access is "limited" by default: reset, mark, console, identify, readpt and dump.
write and install (and any erase) need the full access granted from the page itself.
By default every flash operation returns to the console, so the next boot is captured.

  benchctl.py identify
  benchctl.py readpt [ADDR]
  benchctl.py dump NAME ADDR SIZE [NAME ADDR SIZE ...]
  benchctl.py write PATH@ADDR [PATH@ADDR ...] [--erase]      (full access)
  benchctl.py install MANIFEST [--erase]                     (full access)
  benchctl.py console | reset [ID] | mark TEXT
  benchctl.py autoreset on|off [REGEX]                      page-side EN reset on a matching line
  benchctl.py images [FILTER]                                (server only, no page needed)

PATH is a deliverable as listed by `images` (relative to BENCH_WEB, or dumps/...); its
SHA256 is taken from the server listing and re-checked by the browser before writing.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import uuid

BASE = "http://localhost:%s" % os.environ.get("BENCH_PORT", "8080")


def call(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def num(text):
    return int(text, 16)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("op")
    p.add_argument("args", nargs="*")
    p.add_argument("--erase", action="store_true", help="erase the whole flash first (full access)")
    p.add_argument("--stay", action="store_true", help="stay in the loader instead of reopening the console")
    p.add_argument("--timeout", type=float, default=300)
    a = p.parse_args()

    if a.op == "images":
        inv = call("/api/images")
        for i in sorted(inv["images"], key=lambda i: -i["mtime"]):
            if not a.args or a.args[0] in i["path"]:
                print("%s  %9d  %s  %s" % (time.strftime("%m-%d %H:%M", time.localtime(i["mtime"])), i["size"], i["sha256"], i["path"]))
        for m in inv["manifests"]:
            print("manifest %s: %s" % (m["path"], m["name"]))
        return 0

    # The page drops commands it pulls after this deadline (closed tab: no late flash write).
    cmd = {"op": a.op, "req": uuid.uuid4().hex, "stay": a.stay, "expires": int((time.time() + a.timeout) * 1000)}
    if a.erase:
        cmd["erase"] = True
    if a.op == "readpt":
        cmd["addr"] = num(a.args[0]) if a.args else 0x8000
    elif a.op == "dump":
        if not a.args or len(a.args) % 3:
            p.error("dump NAME ADDR SIZE [...]")
        # NAME-0xADDR, so the page proposes the right address when restoring the read-back.
        cmd["regions"] = [{"name": "%s-0x%x" % (a.args[i], num(a.args[i + 1])), "addr": num(a.args[i + 1]),
                           "size": num(a.args[i + 2])} for i in range(0, len(a.args), 3)]
    elif a.op == "write":
        shas = {i["path"]: i["sha256"] for i in call("/api/images")["images"]}
        cmd["items"] = []
        for item in a.args:
            path, _, addr = item.rpartition("@")
            if path not in shas:
                p.error("not a listed deliverable: " + path)
            cmd["items"].append({"path": path, "addr": num(addr), "sha": shas[path]})
    elif a.op == "install":
        cmd["manifest"] = a.args[0]
    elif a.op == "reset":
        cmd["id"] = a.args[0] if a.args else "benchctl"
    elif a.op == "autoreset":
        cmd["enabled"] = (a.args[0] if a.args else "on") != "off"
        if len(a.args) > 1:
            cmd["pattern"] = " ".join(a.args[1:])
    elif a.op == "mark":
        cmd["text"] = " ".join(a.args)

    call("/api/cmd", cmd)
    deadline = time.monotonic() + a.timeout
    while time.monotonic() < deadline:
        r = call("/api/result?req=" + cmd["req"])
        if r:
            print(json.dumps(r, indent=1, ensure_ascii=False))
            return 0 if r.get("ok") else 1
        time.sleep(0.3)
    print("no answer from the page (closed tab, port released or busy?)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

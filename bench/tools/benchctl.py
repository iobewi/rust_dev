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
  benchctl.py tx HEX | rxhex on|off | io0 [MS]                 (full access) UART bytes, raw rx log, hold BOOT
  benchctl.py http GET|POST URL [BODY]                       (full access) request to a private-LAN device, sent by the browser
  benchctl.py autoreset on|off [REGEX]                      page-side EN reset on a matching line
  benchctl.py images [FILTER] [--all] [--archives]           (server only, no page needed)

PATH is a deliverable as listed by `images` (relative to BENCH_WEB, or dumps/...); its
SHA256 is taken from the server listing and re-checked by the browser before writing.

Lifecycle of a command (server/commands.py) and exit status:
  completed 0 | failed 1 | no answer 2 | indeterminate 3 | expired (never executed) 4
A write that ends `indeterminate` may have erased its region: read it back before anything boots the board.
"no answer" is never read as "not executed": the status asked from the server says which it is.
A command that waits for the page longer than its ttl (default 120 s) expires and is never executed afterwards.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://localhost:%s" % os.environ.get("BENCH_PORT", "8080")


def call(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return dict(json.load(e), http=e.code)
        except ValueError:
            raise e


def num(text):
    return int(text, 16)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("op")
    p.add_argument("args", nargs="*")
    p.add_argument("--erase", action="store_true", help="erase the whole flash first (full access)")
    p.add_argument("--stay", action="store_true", help="stay in the loader instead of reopening the console")
    p.add_argument("--all", action="store_true", help="images: also the folders not declared in BENCH_INVENTORY_DIRS")
    p.add_argument("--archives", action="store_true", help="images: also archived copies")
    p.add_argument("--timeout", type=float, default=300, help="how long to wait for the outcome")
    p.add_argument("--ttl", type=float, default=None, help="seconds a command may wait for the page before it expires unexecuted")
    a = p.parse_args()

    if a.op == "images":
        inv = call("/api/images?fresh=1" + ("&scope=all" if a.all else "") + ("&archives=1" if a.archives else ""))
        for i in sorted(inv["images"], key=lambda i: -i["mtime"]):
            if not a.args or a.args[0] in i["path"]:
                print("%s  %9d  %s  %-8s %s" % (time.strftime("%m-%d %H:%M", time.localtime(i["mtime"])), i["size"], i["sha256"], i["category"], i["path"]))
        for m in inv["manifests"]:
            print("manifest %s: %s" % (m["path"], m["name"]))
        for j in inv.get("ignored_json", []):
            print("ignored json %s: %s" % (j["path"], j["reason"]))
        c, x = inv["counts"], inv["excluded"]
        print("%d files, %d unique sha256 (%d contents listed more than once, none merged); hidden: %d archives, %d outside declared folders"
              % (c["files"], c["unique_sha256"], c["duplicate_groups"], x["archives"], x["outside_allowed"]))
        return 0

    # The server drops a command that waits longer than its ttl (closed tab: no late flash write).
    cmd = {"op": a.op, "req": uuid.uuid4().hex, "stay": a.stay}
    if a.ttl is not None:
        cmd["ttl"] = a.ttl
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
        shas = {i["path"]: i["sha256"] for i in call("/api/images?scope=all&archives=1&fresh=1")["images"]}
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
    elif a.op == "tx":
        cmd["hex"] = "".join(a.args)           # raw bytes, hex (full access); content is not logged
    elif a.op == "rxhex":
        cmd["enabled"] = (a.args[0] if a.args else "on") != "off"
    elif a.op == "io0":
        cmd["ms"] = int(a.args[0]) if a.args else 3000   # hold GPIO0 low (BOOT) for ms, app running
    elif a.op == "http":
        cmd["method"] = a.args[0].upper()
        cmd["url"] = a.args[1]
        if len(a.args) > 2:
            cmd["body"] = a.args[2]          # form body; sent, never logged
    elif a.op == "mark":
        cmd["text"] = " ".join(a.args)

    queued = call("/api/cmd", cmd)
    if not queued.get("queued"):
        print(json.dumps(queued), file=sys.stderr)
        return 2
    deadline = time.monotonic() + a.timeout
    while time.monotonic() < deadline:
        r = call("/api/result?req=" + cmd["req"])
        if r:
            print(json.dumps(r, indent=1, ensure_ascii=False))
            return EXIT.get(r.get("state"), 0 if r.get("ok") else 1)
        time.sleep(0.3)
    st = call("/api/status?req=" + cmd["req"])
    state = st.get("state", "unknown")
    if state in ("queued", "claimed"):
        print("no answer: command %s still %s, NOT executed (closed tab, port released or busy?)" % (cmd["req"], state), file=sys.stderr)
        return 2
    print("no answer: command %s is %s; it WAS started, outcome unknown. Read the flash back before anything boots the board."
          % (cmd["req"], state), file=sys.stderr)
    return 3


EXIT = {"completed": 0, "failed": 1, "indeterminate": 3, "expired": 4}


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Qualification bench server: serves the bench page and relays the browser serial console.

- GET  /...             bench page (bench/web), else data files from BENCH_WEB (manifests, images)
- GET  /dumps/...       flash read-backs saved by the page (BENCH_LOGS/dumps)
- GET  /api/info        where this server writes: capture file, dumps, data directory
- GET  /api/images[?scope=all][&archives=1][&fresh=1][&path=FILE ...]  binaries with SHA256, category (artifact, backup, capture,
                        archive), origin and counts. Default: the folders of BENCH_INVENTORY_DIRS (default p91) and the
                        current read-backs; scope=all adds the other folders, archives=1 the archived copies. ESP Web
                        Tools manifests, and the JSON files that are not (reason given)
- POST /api/log         JSON {"lines": [{"t": <browser ms>, "s": "<line>"}]} appended to the capture
- GET  /api/cmd[?wait=S&page=ID] claim the next queued command for the page, or {} (long poll up to S seconds)
- POST /api/cmd         queue a command {op, req?, ttl?, ...}; 409 if req already exists (container only, see benchctl.py)
- POST /api/ack         {req, claim, page}: the page starts that claimed command (409: it must NOT run it)
- POST /api/release     {req, claim}: a claimed command the page cannot start now goes back to the queue
- POST /api/result      outcome of a command carrying "req", posted by the page (kept after the HTTP client left)
- GET  /api/result?req= that outcome, or {} while pending
- GET  /api/status?req= lifecycle (queued/claimed/running/completed/failed/indeterminate/expired), no payload
Lifecycle and guarantees: commands.py.
- POST /api/dump?name=  raw flash read-back; saved as dumps/<name>.bin and archived with a timestamp

Stdlib only. No device access from the container: the browser owns the port.
"""
import hashlib
import json
import os
import select
import socket
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAGES = Path(__file__).resolve().parent.parent / "web"
WEB = Path(os.environ.get("BENCH_WEB", "/workspace/web")).resolve()
LOGS = Path(os.environ.get("BENCH_LOGS", str(PAGES.parent / "logs"))).resolve()
DUMPS = LOGS / "dumps"
DUMPS.mkdir(parents=True, exist_ok=True)
CAPTURE = LOGS / "serial.log"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from commands import Commands  # noqa: E402
from inventory import Inventory  # noqa: E402

capture_lock = threading.Lock()   # appends to the capture and the event log, nothing else (no disk walk under it)
commands = Commands(ttl=float(os.environ.get("BENCH_CMD_TTL", "120")), ack_timeout=float(os.environ.get("BENCH_ACK_TIMEOUT", "15")))
EVENTS = LOGS / "commands.jsonl"
inventory = Inventory(WEB, DUMPS, allow=[d for d in os.environ.get("BENCH_INVENTORY_DIRS", "p91").split(",") if d])


def write_events():
    """Lifecycle events (op, req, state, reason; never a payload), appended outside the command lock."""
    events = commands.drain_events()
    if events:
        with capture_lock, EVENTS.open("a", encoding="utf-8") as out:
            for e in events:
                out.write(json.dumps(e) + "\n")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def translate_path(self, path):
        # Versioned page wins; read-backs come from the logs; the rest from the runtime directory.
        local = Path(super().translate_path(path))
        rel = local.relative_to(WEB)
        if rel.parts[:1] == ("dumps",):
            return str(DUMPS.joinpath(*rel.parts[1:]))
        page = PAGES / rel
        if page.is_dir():
            page = page / "index.html"
        return str(page) if page.is_file() else str(local)

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _client_alive(self):
        # Readable with no data = the browser closed this connection.
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return not ready or self.connection.recv(1, socket.MSG_PEEK) != b""
        except OSError:
            return False

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/cmd"):
            # Long poll: the page waits on a pending request, not on timers that a
            # background tab would throttle to once a minute.
            # A reloaded or closed page leaves its long poll here: never hand a command to a
            # client that has gone, check the socket between short waits.
            q = parse_qs(urlparse(self.path).query)
            deadline = time.monotonic() + min(float(q.get("wait", ["0"])[0] or 0), 30)
            page = q.get("page", [""])[0]
            while True:
                if not self._client_alive():
                    return
                if not page:
                    # A page from before the acknowledgement protocol (tab left open across an upgrade) would run a
                    # command without acknowledging it. It gets nothing; it must be reloaded.
                    time.sleep(max(0.0, min(0.5, deadline - time.monotonic())))
                    if time.monotonic() >= deadline:
                        return self._json(200, {})
                    continue
                cmd = commands.claim(page, wait=max(0.0, min(0.5, deadline - time.monotonic())))
                if cmd:
                    write_events()
                    try:
                        return self._json(200, cmd)
                    except OSError:
                        # Nothing proves the page saw it, and it cannot have acknowledged: back in the queue.
                        commands.release(cmd["req"], cmd["claim"])
                        write_events()
                        return
                if time.monotonic() >= deadline:
                    write_events()
                    return self._json(200, {})
        if self.path.startswith("/api/result?req="):
            return self._json(200, commands.result(self.path.split("=", 1)[1]))
        if self.path.startswith("/api/status?req="):
            return self._json(200, commands.status(self.path.split("=", 1)[1]))
        if self.path == "/api/queue":
            return self._json(200, commands.summary())
        if self.path == "/api/info":
            return self._json(200, {"capture": str(CAPTURE), "dumps": str(DUMPS), "web": str(WEB), "pages": str(PAGES)})
        if self.path.startswith("/api/images"):
            # fresh=1: rebuild now (the caller has just put a file there); otherwise the snapshot is at most ~1 s old.
            q = parse_qs(urlparse(self.path).query)
            snap = inventory.get(scope="all" if q.get("scope") == ["all"] else "default",
                                 archives=q.get("archives") == ["1"], fresh=q.get("fresh") == ["1"])
            if "path" in q:
                # path=<file> (repeatable): only the named entries. A caller that needs its own deliverables does not
                # have to carry the whole listing, which grows with every read-back.
                want = set(q["path"])
                snap = {**snap, "images": [i for i in snap["images"] if i["path"] in want]}
            return self._json(200, snap)
        return super().do_GET()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        if self.path == "/api/log":
            payload = json.loads(body or b"{}")
            now = time.time()
            with capture_lock, CAPTURE.open("a", encoding="utf-8") as out:
                for line in payload.get("lines", []):
                    stamp = time.strftime("%H:%M:%S", time.localtime(now)) + ".%03d" % (now % 1 * 1000)
                    out.write("%s %s\n" % (stamp, line.get("s", "")))
            return self._json(200, {"ok": True})
        if self.path.startswith("/api/dump?name="):
            name = self.path.split("=", 1)[1]
            if not name.replace("-", "").replace("_", "").isalnum():
                return self._json(400, {})
            stamp = time.strftime("%Y%m%d-%H%M%S")
            (DUMPS / "archive").mkdir(exist_ok=True)
            (DUMPS / (name + ".bin")).write_bytes(body)
            (DUMPS / "archive" / ("%s-%s.bin" % (name, stamp))).write_bytes(body)
            inventory.invalidate()
            return self._json(200, {"saved": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                                    "archive": "dumps/archive/%s-%s.bin" % (name, stamp)})
        if self.path in ("/api/result", "/api/cmd", "/api/ack", "/api/release"):
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid json"})
            if self.path == "/api/result":
                code, out = commands.finish(payload)
            elif self.path == "/api/cmd":
                code, out = commands.enqueue(payload)
            elif self.path == "/api/ack":
                code, out = commands.ack(str(payload.get("req", "")), payload.get("claim"), payload.get("page"))
            else:
                code, out = commands.release(str(payload.get("req", "")), payload.get("claim"))
            write_events()
            return self._json(code, out)
        return self._json(404, {})


def acquire_instance_lock(port):
    """One server per capture directory (two benches, e.g. UART and native USB, have two). Never kills anything."""
    import fcntl
    f = (LOGS / ".bench_server.lock").open("a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        sys.exit("bench_server: another instance already serves %s (%s); not starting a second one" % (LOGS, f.read().strip()))
    f.seek(0)
    f.truncate()
    f.write("pid=%d port=%d\n" % (os.getpid(), port))
    f.flush()
    return f


if __name__ == "__main__":
    port = int(os.environ.get("BENCH_PORT", "8080"))
    instance = acquire_instance_lock(port)     # kept open for the life of the process
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        sys.exit("bench_server: cannot listen on :%d (%s); something else owns that port" % (port, e))
    print("bench server on :%d, pages=%s, data=%s, capture=%s" % (port, PAGES, WEB, CAPTURE), flush=True)
    httpd.serve_forever()

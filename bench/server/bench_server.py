#!/usr/bin/env python3
"""Qualification bench server: serves the bench page and relays the browser serial console.

- GET  /...             bench page (bench/web), else data files from BENCH_WEB (manifests, images)
- GET  /dumps/...       flash read-backs saved by the page (BENCH_LOGS/dumps)
- GET  /api/info        where this server writes: capture file, dumps, data directory
- GET  /api/images      deliverable binaries (*.bin under BENCH_WEB and dumps) with SHA256,
                        and ESP Web Tools manifests found under BENCH_WEB
- POST /api/log         JSON {"lines": [{"t": <browser ms>, "s": "<line>"}]} appended to the capture
- GET  /api/cmd[?wait=S] next pending command for the page, or {} (long poll up to S seconds)
- POST /api/cmd         queue a command (used from the container only; see tools/benchctl.py)
- POST /api/result      outcome of a command carrying "req", posted by the page
- GET  /api/result?req= that outcome, or {} while pending
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

PAGES = Path(__file__).resolve().parent.parent / "web"
WEB = Path(os.environ.get("BENCH_WEB", "/workspace/web")).resolve()
LOGS = Path(os.environ.get("BENCH_LOGS", str(PAGES.parent / "logs"))).resolve()
DUMPS = LOGS / "dumps"
DUMPS.mkdir(parents=True, exist_ok=True)
CAPTURE = LOGS / "serial.log"
lock = threading.Lock()
queued = threading.Condition(lock)
commands = []
results = {}  # req -> outcome posted by the page (bounded)
digests = {}  # path -> (mtime_ns, size, sha256)


def sha256_of(path):
    st = path.stat()
    key = str(path)
    cached = digests.get(key)
    if cached and cached[:2] == (st.st_mtime_ns, st.st_size):
        return cached[2]
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digests[key] = (st.st_mtime_ns, st.st_size, h.hexdigest())
    return h.hexdigest()


def visible(root):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and not any(part.startswith(".") for part in rel.parts):
            yield path, rel


def inventory():
    images, manifests = [], []
    for root, prefix in ((WEB, ""), (DUMPS, "dumps/")):
        for path, rel in visible(root):
            if prefix == "" and rel.parts[0] == "dumps":
                continue
            url = prefix + rel.as_posix()
            if path.suffix == ".bin":
                st = path.stat()
                images.append({"path": url, "size": st.st_size, "mtime": int(st.st_mtime),
                               "sha256": sha256_of(path), "source": "dumps" if prefix else "web"})
            elif path.suffix == ".json" and not prefix:
                try:
                    m = json.loads(path.read_text(encoding="utf-8"))
                    parts = m["builds"][0]["parts"]
                    manifests.append({"path": url, "name": m.get("name", url),
                                      "prompt_erase": bool(m.get("new_install_prompt_erase")),
                                      "chip": m["builds"][0].get("chipFamily", ""),
                                      "parts": [{"path": p["path"], "offset": p["offset"]} for p in parts]})
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
    return {"images": images, "manifests": manifests}


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
            # client that has gone (it would be lost), check the socket between short waits.
            deadline = time.monotonic() + min(float(self.path.split("wait=", 1)[1]) if "wait=" in self.path else 0, 30)
            while True:
                if not self._client_alive():
                    return
                with queued:
                    queued.wait_for(lambda: commands, timeout=max(0, min(0.5, deadline - time.monotonic())))
                    if commands and self._client_alive():
                        return self._json(200, commands.pop(0))
                if time.monotonic() >= deadline:
                    return self._json(200, {})
        if self.path.startswith("/api/result?req="):
            with lock:
                return self._json(200, results.get(self.path.split("=", 1)[1], {}))
        if self.path == "/api/info":
            return self._json(200, {"capture": str(CAPTURE), "dumps": str(DUMPS), "web": str(WEB), "pages": str(PAGES)})
        if self.path == "/api/images":
            with lock:
                return self._json(200, inventory())
        return super().do_GET()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        if self.path == "/api/log":
            payload = json.loads(body or b"{}")
            now = time.time()
            with lock, CAPTURE.open("a", encoding="utf-8") as out:
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
            return self._json(200, {"saved": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                                    "archive": "dumps/archive/%s-%s.bin" % (name, stamp)})
        if self.path == "/api/result":
            outcome = json.loads(body or b"{}")
            with lock:
                results[str(outcome.get("req", ""))] = outcome
                while len(results) > 200:
                    results.pop(next(iter(results)))
            return self._json(200, {"ok": True})
        if self.path == "/api/cmd":
            with queued:
                commands.append(json.loads(body))
                queued.notify_all()
            return self._json(200, {"queued": True})
        return self._json(404, {})


if __name__ == "__main__":
    port = int(os.environ.get("BENCH_PORT", "8080"))
    print("bench server on :%d, pages=%s, data=%s, capture=%s" % (port, PAGES, WEB, CAPTURE), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

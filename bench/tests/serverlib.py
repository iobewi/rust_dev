"""Starts the real bench server on an ephemeral port in this process, with throw-away data and log directories."""
import http.client
import importlib
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"


class Bench:
    def __init__(self, **env):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "web").mkdir()
        (root / "logs").mkdir()
        self.web, self.logs = root / "web", root / "logs"
        os.environ.update({"BENCH_WEB": str(self.web), "BENCH_LOGS": str(self.logs), "BENCH_CMD_TTL": "120", "BENCH_ACK_TIMEOUT": "15"})
        os.environ.update({k: str(v) for k, v in env.items()})
        sys.path.insert(0, str(SERVER_DIR))
        sys.modules.pop("bench_server", None)
        sys.modules.pop("commands", None)
        sys.modules.pop("inventory", None)
        self.mod = importlib.import_module("bench_server")
        self.httpd = self.mod.ThreadingHTTPServer(("127.0.0.1", 0), self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def request(self, method, path, body=None, timeout=10):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            c.request(method, path, None if body is None else json.dumps(body), {"Content-Type": "application/json"})
            r = c.getresponse()
            raw = r.read()
            try:
                return r.status, json.loads(raw or b"{}")
            except ValueError:
                return r.status, raw
        finally:
            c.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body, **kw):
        return self.request("POST", path, body, **kw)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

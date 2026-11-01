"""Binary inventory for /api/images, built without holding any lock shared with the command or capture paths.

Disk work (walk, stat, SHA-256 of new or changed files) happens in the calling thread only, one build at a time:
callers that arrive during a build get the previous snapshot instead of queuing behind it (the first call, which
has none, waits). Hashes are cached by (mtime, size). The snapshot is shared and treated as read-only.
"""
import hashlib
import json
import threading
import time
from pathlib import Path


def visible(root):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and not any(part.startswith(".") for part in rel.parts):
            yield path, rel


class Inventory:
    def __init__(self, web, dumps, ttl=1.0, clock=time.monotonic):
        self.web, self.dumps, self.ttl, self.clock = Path(web), Path(dumps), ttl, clock
        self.digests = {}                     # path -> (mtime_ns, size, sha256)
        self._state = threading.Lock()        # snapshot fields only, never held during disk work
        self._build_lock = threading.Lock()   # one build at a time
        self._snap, self._at = None, 0.0

    def sha256_of(self, path):
        st = path.stat()
        key = str(path)
        cached = self.digests.get(key)
        if cached and cached[:2] == (st.st_mtime_ns, st.st_size):
            return cached[2]
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        self.digests[key] = (st.st_mtime_ns, st.st_size, h.hexdigest())
        return h.hexdigest()

    def invalidate(self):
        with self._state:
            self._at = float("-inf")

    def get(self, fresh=False):
        while True:
            with self._state:
                snap, at = self._snap, self._at
            if snap is not None and not fresh and self.clock() - at < self.ttl:
                return snap
            if self._build_lock.acquire(blocking=(snap is None or fresh)):
                try:
                    with self._state:
                        if self._snap is not snap and not fresh and self.clock() - self._at < self.ttl:
                            return self._snap     # built by the thread we waited for
                    new = self.build()
                    with self._state:
                        self._snap, self._at = new, self.clock()
                    return new
                finally:
                    self._build_lock.release()
            return snap                            # a build is running: serve the previous one

    def build(self):
        images, manifests = [], []
        for root, prefix in ((self.web, ""), (self.dumps, "dumps/")):
            for path, rel in visible(root):
                if prefix == "" and rel.parts[0] == "dumps":
                    continue
                url = prefix + rel.as_posix()
                try:
                    if path.suffix == ".bin":
                        st = path.stat()
                        images.append({"path": url, "size": st.st_size, "mtime": int(st.st_mtime),
                                       "sha256": self.sha256_of(path), "source": "dumps" if prefix else "web"})
                    elif path.suffix == ".json" and not prefix:
                        m = json.loads(path.read_text(encoding="utf-8"))
                        parts = m["builds"][0]["parts"]
                        manifests.append({"path": url, "name": m.get("name", url),
                                          "prompt_erase": bool(m.get("new_install_prompt_erase")),
                                          "chip": m["builds"][0].get("chipFamily", ""),
                                          "parts": [{"path": p["path"], "offset": p["offset"]} for p in parts]})
                except FileNotFoundError:
                    continue                       # moved or removed while walking
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
        return {"images": images, "manifests": manifests}

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


ARCHIVE_NAMES = ("archive", "archives")


def classify(rel, source):
    """capture = fresh read-back; backup = restore material; archive = kept aside, off by default; artifact = to program."""
    if source == "dumps":
        return "archive" if rel.parts[0] in ARCHIVE_NAMES else "capture"
    folders = [p.lower() for p in rel.parts[:-1]]
    if any(n in ARCHIVE_NAMES for n in folders):
        return "archive"
    if any(n.startswith("backup") for n in folders):
        return "backup"
    return "artifact"


class Inventory:
    """`allow`: top-level folders of the data directory that are programmable by default (BENCH_INVENTORY_DIRS)."""

    def __init__(self, web, dumps, allow=("p91",), ttl=1.0, clock=time.monotonic):
        self.web, self.dumps, self.ttl, self.clock = Path(web), Path(dumps), ttl, clock
        self.allow = tuple(allow)
        self.digests = {}                     # path -> (mtime_ns, size, sha256)
        self._state = threading.Lock()        # snapshot fields only, never held during disk work
        self._build_locks = {}                # one build at a time per view
        self._views = {}                      # (scope, archives) -> (snapshot, built_at)

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
            self._views = {k: (snap, float("-inf")) for k, (snap, _) in self._views.items()}

    def get(self, scope="default", archives=False, fresh=False):
        key = (scope, bool(archives))
        with self._state:
            build_lock = self._build_locks.setdefault(key, threading.Lock())
        while True:
            with self._state:
                snap, at = self._views.get(key, (None, 0.0))
            if snap is not None and not fresh and self.clock() - at < self.ttl:
                return snap
            if build_lock.acquire(blocking=(snap is None or fresh)):
                try:
                    with self._state:
                        cur, cur_at = self._views.get(key, (None, 0.0))
                    if cur is not snap and not fresh and self.clock() - cur_at < self.ttl:
                        return cur                # built by the thread we waited for
                    new = self.build(scope, archives)
                    with self._state:
                        self._views[key] = (new, self.clock())
                    return new
                finally:
                    build_lock.release()
            return snap                            # a build is running: serve the previous one

    def build(self, scope="default", archives=False):
        """Files that are listed are hashed; the others are only counted. Nothing is deduplicated or merged:
        two files with the same SHA-256 are two entries (role, origin and target address may differ)."""
        images, manifests, ignored = [], [], []
        excluded = {"archives": 0, "outside_allowed": 0}
        for root, source in ((self.web, "web"), (self.dumps, "dumps")):
            for path, rel in visible(root):
                if source == "web" and rel.parts[0] == "dumps":
                    continue
                url = ("dumps/" if source == "dumps" else "") + rel.as_posix()
                category = classify(rel, source)
                origin = "dumps" if source == "dumps" else (rel.parts[0] if len(rel.parts) > 1 else ".")
                allowed = source == "dumps" or origin in self.allow
                is_bin, is_json = path.suffix == ".bin", path.suffix == ".json" and source == "web"
                if not (is_bin or is_json):
                    continue
                if category == "archive" and not archives:
                    excluded["archives"] += is_bin
                    continue
                if not allowed and scope != "all":
                    excluded["outside_allowed"] += is_bin
                    continue
                try:
                    if is_bin:
                        st = path.stat()
                        images.append({"path": url, "size": st.st_size, "mtime": int(st.st_mtime), "sha256": self.sha256_of(path),
                                       "source": source, "category": category, "origin": origin, "allowed": allowed})
                    else:
                        try:
                            m = json.loads(path.read_text(encoding="utf-8"))
                        except ValueError:
                            ignored.append({"path": url, "reason": "invalid-json"})
                            continue
                        try:
                            parts = m["builds"][0]["parts"]
                            manifests.append({"path": url, "name": m.get("name", url),
                                              "prompt_erase": bool(m.get("new_install_prompt_erase")),
                                              "chip": m["builds"][0].get("chipFamily", ""),
                                              "parts": [{"path": p["path"], "offset": p["offset"]} for p in parts]})
                        except (KeyError, IndexError, TypeError):
                            ignored.append({"path": url, "reason": "not-esp-web-tools (no builds[0].parts)"})
                except FileNotFoundError:
                    continue                       # moved or removed while walking
        by_sha = {}
        for i in images:
            by_sha[i["sha256"]] = by_sha.get(i["sha256"], 0) + 1
        by_category = {}
        for i in images:
            by_category[i["category"]] = by_category.get(i["category"], 0) + 1
        return {"images": images, "manifests": manifests, "ignored_json": ignored,
                "counts": {"files": len(images), "unique_sha256": len(by_sha),
                           "duplicate_groups": sum(1 for n in by_sha.values() if n > 1), "by_category": by_category},
                "excluded": excluded,
                "scope": {"scope": scope, "archives": bool(archives), "allowed_dirs": list(self.allow)}}

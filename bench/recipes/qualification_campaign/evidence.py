"""Persistent evidence storage (G1).

A destructive campaign keeps the only copy of the pre-write reference (backup) in its evidence folder. In the devcontainer the container
filesystem is an overlay that a rebuild discards (only the declared volumes survive), so such a folder must live on a mount that is not
ephemeral. The decision is a pure function of the mount table, so it is testable without touching the real file system.
"""
import json
import os
from pathlib import Path

import pytest

EPHEMERAL_FS = {"overlay", "tmpfs", "ramfs", "squashfs", "aufs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2"}


def _unescape(field):
    """mountinfo escapes space, tab, newline and backslash as octal."""
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def parse_mountinfo(text):
    """[(mount_point, fstype, source)] from /proc/self/mountinfo text. Malformed lines are skipped."""
    mounts = []
    for line in text.splitlines():
        left, separator, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if not separator or len(fields) < 5 or len(tail) < 2:
            continue
        mounts.append((_unescape(fields[4]), tail[0], _unescape(tail[1])))
    return mounts


def mount_of(path, mounts):
    """The mount that holds `path`: the one with the longest mount point that is a prefix of it."""
    path = str(path)
    best = None
    for mount_point, fstype, source in mounts:
        inside = path == mount_point or mount_point == "/" or path.startswith(mount_point.rstrip("/") + "/")
        if inside and (best is None or len(mount_point) >= len(best[0])):
            best = (mount_point, fstype, source)
    return best


def check_persistent_root(root, mountinfo=None):
    """(ok, reason, detail). ok is True only if the folder lives on a filesystem that survives the container.

    The root is resolved first (symbolic links followed), so a link into an ephemeral area cannot pass. An unknown mount, an
    empty mount table or an ephemeral filesystem all refuse: this is a protection of irreplaceable evidence, not a convenience."""
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text()
        except OSError:
            return False, "the mount table cannot be read: persistence cannot be established", {}
    resolved = os.path.realpath(str(root))
    mount = mount_of(resolved, parse_mountinfo(mountinfo))
    if mount is None:
        return False, "no mount holds the evidence root in the mount table", {"resolved": resolved}
    mount_point, fstype, source = mount
    detail = {"resolved": resolved, "mount_point": mount_point, "fstype": fstype, "source": source}
    if fstype in EPHEMERAL_FS:
        return False, f"the evidence root is on an ephemeral filesystem ({fstype} mounted at {mount_point})", detail
    return True, f"persistent filesystem {fstype} mounted at {mount_point}", detail


def require_persistent_evidence(cfg, pytestconfig):
    """BLOCKED unless the evidence root is persistent, or the development override is given (and is then recorded by the recipe)."""
    ok, reason, detail = check_persistent_root(cfg["evidence_root"])
    if ok:
        return {"persistent": True, "reason": reason, **detail, "override": False}
    if pytestconfig.getoption("--qual-allow-ephemeral-evidence", default=False):
        return {"persistent": False, "reason": reason, **detail, "override": True}
    pytest.skip("BLOCKED: " + reason + "; use a persistent volume (docs/qualification-p4-c6.md) "
                "- the backup of a destructive campaign must survive a container rebuild")


def record_storage(case, storage):
    case.write("evidence-storage.json", storage)
    return json.dumps({k: storage[k] for k in ("persistent", "override")})

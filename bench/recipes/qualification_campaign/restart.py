"""Identification and observation of the real bench launcher (Linux /proc). No network, no secrets.

A pid file is only a hint. The launcher is accepted when (1) its command line is tools/run_server.py,
(2) it has exactly one child running server/bench_server.py, and (3) that child owns the listening
socket of the bench URL port. The lock file and the journal are never touched here.
"""
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")


class LauncherError(RuntimeError):
    pass


def cmdline(pid):
    try:
        return (PROC / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")[:-1]
    except OSError:
        return None


def start_ticks(pid):
    try:
        raw = (PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    return int(raw.rsplit(")", 1)[1].split()[19])          # field 22: process start time


def children(pid):
    found = []
    for entry in PROC.iterdir():
        if entry.name.isdigit():
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            if int(stat.rsplit(")", 1)[1].split()[1]) == pid:
                found.append(int(entry.name))
    return sorted(found)


def listening_pids(port):
    inodes = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = (PROC / "net" / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if parts[3] == "0A" and int(parts[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(parts[9])
    owners = set()
    for entry in PROC.iterdir():
        if entry.name.isdigit():
            try:
                for fd in (entry / "fd").iterdir():
                    target = os.readlink(fd)
                    if target.startswith("socket:[") and target[8:-1] in inodes:
                        owners.add(int(entry.name))
            except OSError:
                continue
    return owners


@dataclass(frozen=True)
class Launcher:
    pid: int
    ticks: int
    child: int
    child_ticks: int


def identify_launcher(pid_file, port):
    try:
        pid = int(Path(pid_file).read_text().strip())
    except (OSError, ValueError) as error:
        raise LauncherError("Launcher pid file unreadable") from error
    line = cmdline(pid)
    if not line or not any(part.endswith("run_server.py") for part in line):
        raise LauncherError("Pid file does not designate a run_server.py launcher; refusing to signal")
    kids = children(pid)
    servers = [k for k in kids if any(p.endswith("bench_server.py") for p in cmdline(k) or [])]
    if len(servers) != 1:
        raise LauncherError(f"Expected exactly one server child, found {len(servers)}")
    if servers[0] not in listening_pids(port):
        raise LauncherError("The server child does not own the bench URL listening socket")
    return Launcher(pid, start_ticks(pid), servers[0], start_ticks(servers[0]))


def request_restart(launcher):
    """SIGHUP the launcher only if it is still the very process that was identified."""
    if start_ticks(launcher.pid) != launcher.ticks or not any(p.endswith("run_server.py") for p in cmdline(launcher.pid) or []):
        raise LauncherError("Launcher identity changed since identification; no signal sent")
    os.kill(launcher.pid, signal.SIGHUP)


def child_gone(launcher):
    return start_ticks(launcher.child) != launcher.child_ticks


def replacement(launcher):
    """The new server child of the SAME launcher, or None."""
    if start_ticks(launcher.pid) != launcher.ticks:
        raise LauncherError("Launcher itself changed or exited")
    fresh = [k for k in children(launcher.pid) if k != launcher.child and any(
        p.endswith("bench_server.py") for p in cmdline(k) or [])]
    return fresh[0] if len(fresh) == 1 else None


def storage_fingerprint(state_dir):
    base = Path(state_dir)
    db, lock = base / "operations.sqlite3", base / "server.lock"
    return {"db_inode": db.stat().st_ino, "db_size": db.stat().st_size, "lock_inode": lock.stat().st_ino}


def wait_for(predicate, timeout, interval=0.5):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)

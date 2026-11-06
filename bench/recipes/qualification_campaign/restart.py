"""Identification and observation of the real bench launcher (Linux /proc). No network, no secrets.

Nothing is signalled unless ONE server is established by three independent facts:
  1. the bench URL is a loopback endpoint (a remote URL never designates a local process);
  2. the launcher runs tools/run_server.py with exactly one child running server/bench_server.py, and that
     child owns the listening socket of the URL's port (bound to loopback or wildcard);
  3. that same child holds open `server.lock` and `operations.sqlite3` of the configured state_dir.
The pid file is only a hint. The lock file and the journal are never touched here.
"""
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

PROC = Path("/proc")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
LISTEN_OK = {"0100007F", "00000000", "00000000000000000000000001000000", "00000000000000000000000000000000"}


class LauncherError(RuntimeError):
    pass


class UnsupportedEndpoint(LauncherError):
    pass


class OrderError(LauncherError):
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


def is_server(pid):
    return any(part.endswith("bench_server.py") for part in cmdline(pid) or [])


def children(pid):
    try:                                                    # direct and cheap when the kernel exposes it
        return sorted(int(x) for x in (PROC / str(pid) / "task" / str(pid) / "children").read_text().split())
    except OSError:
        pass
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


def local_port(url):
    parts = urlparse(url)
    if parts.scheme not in {"http", "https"} or (parts.hostname or "") not in {h.strip("[]") for h in LOOPBACK_HOSTS}:
        raise UnsupportedEndpoint("Only a loopback bench URL can designate a local launcher; refusing to signal")
    return parts.port or (443 if parts.scheme == "https" else 80)


def listening_pids(port):
    inodes = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = (PROC / "net" / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            address, _, hex_port = parts[1].rpartition(":")
            if parts[3] == "0A" and int(hex_port, 16) == port and address in LISTEN_OK:
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


def open_paths(pid):
    paths = set()
    try:
        for fd in (PROC / str(pid) / "fd").iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("/"):
                paths.add(target)
    except OSError:
        pass
    return paths


@dataclass(frozen=True)
class Launcher:
    pid: int
    ticks: int
    child: int
    child_ticks: int
    state_dir: str
    port: int


def identify_launcher(pid_file, url, state_dir):
    port = local_port(url)                                  # (1) loopback endpoint only
    try:
        pid = int(Path(pid_file).read_text().strip())
    except (OSError, ValueError) as error:
        raise LauncherError("Launcher pid file unreadable") from error
    line = cmdline(pid)
    if not line or not any(part.endswith("run_server.py") for part in line):
        raise LauncherError("Pid file does not designate a run_server.py launcher; refusing to signal")
    servers = [k for k in children(pid) if is_server(k)]
    if len(servers) != 1:
        raise LauncherError(f"Expected exactly one server child, found {len(servers)}")
    child = servers[0]
    if child not in listening_pids(port):                   # (2) endpoint <-> process
        raise LauncherError("The server child does not own the bench URL listening socket")
    base = Path(state_dir).resolve()
    held = open_paths(child)
    if not {str(base / "server.lock"), str(base / "operations.sqlite3")} <= held:   # (3) process <-> storage
        raise LauncherError("The server child does not hold the configured state_dir lock and journal open")
    return Launcher(pid, start_ticks(pid), child, start_ticks(child), str(base), port)


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
    fresh = [k for k in children(launcher.pid) if k != launcher.child and is_server(k)]
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


# --- ordering evidence --------------------------------------------------------------------------
STARTED = re.compile(r"Bench server child started \(PID (\d+)\)")
EXITED = re.compile(r"Server PID (\d+) exited \((-?\d+)\)")


def verify_order(log_text, old_pid):
    """The launcher's own event log must show the old child exited BEFORE any other child started."""
    events = []
    for number, line in enumerate(log_text.splitlines()):
        if (m := EXITED.search(line)):
            events.append((number, "exited", int(m.group(1))))
        elif (m := STARTED.search(line)):
            events.append((number, "started", int(m.group(1))))
        elif "replacement refused" in line:
            raise OrderError("Launcher refused the replacement (old child did not exit)")
    exits = [n for n, kind, pid in events if kind == "exited" and pid == old_pid]
    starts = [n for n, kind, pid in events if kind == "started" and pid != old_pid]
    if not exits:
        raise OrderError("No 'exited' event for the old child in the launcher log")
    if not starts:
        raise OrderError("No replacement 'started' event in the launcher log")
    if min(starts) < exits[0]:
        raise OrderError("A replacement was started before the old child exited")
    return {"old_exit_line": exits[0], "new_start_line": min(starts)}


class RestartWatch:
    """Samples the launcher's server children; flags any instant with two live servers.

    Sampling is not continuous (interval seconds): a miss cannot be excluded, which is why the
    launcher log order is the primary proof and this is corroborating evidence only."""
    def __init__(self, launcher, interval=0.01):
        self.launcher, self.interval = launcher, interval
        self.samples, self.overlap, self._stop = 0, False, threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            alive = [k for k in children(self.launcher.pid) if is_server(k)]
            self.samples += 1
            self.overlap = self.overlap or len(alive) >= 2
            time.sleep(self.interval)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        return {"samples": self.samples, "overlap_seen": self.overlap, "interval_s": self.interval}

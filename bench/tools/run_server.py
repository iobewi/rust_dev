#!/usr/bin/env python3
"""Foreground server launcher. SIGHUP restarts only its own child, without replay."""
import argparse
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


class ServerLauncher:
    def __init__(self, command, *, shutdown_timeout=30, env=None):
        self.command, self.shutdown_timeout, self.env = command, shutdown_timeout, env
        self.requested = None
        self.child = None

    def request(self, action):
        # A stop always takes precedence over a pending restart.
        if self.requested != 'stop':
            self.requested = action

    def run(self):
        while True:
            if self.requested == 'stop':
                return 0
            self.child = subprocess.Popen(self.command, env=self.env, start_new_session=True)
            print(f'Bench server child started (PID {self.child.pid}).', file=sys.stderr, flush=True)
            deadline = None
            while self.child.poll() is None:
                if self.requested and deadline is None:
                    print(f'Waiting for server PID {self.child.pid} to exit '
                          f'(up to {self.shutdown_timeout:g}s); no replacement starts before exit.',
                          file=sys.stderr, flush=True)
                    try:
                        self.child.terminate()
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + self.shutdown_timeout
                if deadline is not None and time.monotonic() >= deadline:
                    # Check once more at the deadline; never start a second writer.
                    if self.child.poll() is None:
                        print(f'Server PID {self.child.pid} has not exited; replacement refused. '
                              'The process, lock and journal are preserved. Inspect that process locally.',
                              file=sys.stderr, flush=True)
                        return 124
                try:
                    self.child.wait(timeout=.05)
                except subprocess.TimeoutExpired:
                    pass
            code = self.child.returncode
            print(f'Server PID {self.child.pid} exited ({code}).', file=sys.stderr, flush=True)
            if self.requested != 'restart':
                return 0 if self.requested == 'stop' else (code if code >= 0 else 128 - code)
            self.requested = None


def seconds(value):
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 600:
        raise argparse.ArgumentTypeError('Expected a duration between 0 and 600 seconds')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shutdown-timeout', type=seconds, default=30)
    parser.add_argument('--lock-timeout', type=seconds, default=10)
    args = parser.parse_args()
    env = dict(os.environ, BENCH_LOCK_TIMEOUT=str(args.lock_timeout))
    launcher = ServerLauncher([sys.executable, str(Path(__file__).resolve().parents[1] / 'server/bench_server.py')],
                              shutdown_timeout=args.shutdown_timeout, env=env)
    signal.signal(signal.SIGHUP, lambda *_: launcher.request('restart'))
    signal.signal(signal.SIGTERM, lambda *_: launcher.request('stop'))
    signal.signal(signal.SIGINT, lambda *_: launcher.request('stop'))
    print(f'Bench launcher PID {os.getpid()}: SIGHUP restarts, SIGTERM/SIGINT stop.', file=sys.stderr, flush=True)
    return launcher.run()


if __name__ == '__main__':
    sys.exit(main())

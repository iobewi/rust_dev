"""Contention: the inventory must not delay commands or the capture; one server per capture directory."""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from serverlib import Bench, SERVER_DIR


class InventoryContention(unittest.TestCase):
    def test_11_hundreds_of_files_do_not_stall_commands_or_the_capture(self):
        b = Bench()
        try:
            for i in range(600):
                (b.web / "p91").mkdir(exist_ok=True)
                (b.web / "p91" / f"f{i:03d}.bin").write_bytes(os.urandom(256) + i.to_bytes(2, "big"))
            real = b.mod.inventory.sha256_of
            b.mod.inventory.sha256_of = lambda path: (time.sleep(0.005), real(path))[1]      # a cold, slow disk: ~3 s build
            started = time.monotonic()
            t = threading.Thread(target=lambda: b.get("/api/images", timeout=30))
            t.start()
            time.sleep(0.3)
            worst = 0.0
            for i in range(10):
                t0 = time.monotonic()
                self.assertEqual(b.post("/api/log", {"lines": [{"s": f"line {i}"}]})[0], 200)
                self.assertEqual(b.post("/api/cmd", {"op": "mark", "req": f"m{i}"})[0], 200)
                self.assertEqual(b.get("/api/status?req=m%d" % i)[0], 200)
                worst = max(worst, time.monotonic() - t0)
                time.sleep(0.1)
            self.assertTrue(t.is_alive(), "the build should still be running for this test to mean something")
            self.assertLess(worst, 0.25)
            t.join()
            self.assertGreater(time.monotonic() - started, 2.0)
        finally:
            b.close()

    def test_a_second_caller_during_a_build_gets_the_previous_snapshot(self):
        b = Bench()
        try:
            (b.web / "p91").mkdir()
            (b.web / "p91" / "a.bin").write_bytes(b"x")
            self.assertEqual(len(b.get("/api/images")[1]["images"]), 1)
            (b.web / "p91" / "b.bin").write_bytes(b"y")
            real = b.mod.inventory.build
            def slow(*a):
                time.sleep(1.0)
                return real(*a)
            b.mod.inventory.build = slow
            b.mod.inventory.invalidate()
            t = threading.Thread(target=lambda: b.get("/api/images", timeout=30)); t.start()
            time.sleep(0.2)
            t0 = time.monotonic()
            n = len(b.get("/api/images")[1]["images"])
            self.assertLess(time.monotonic() - t0, 0.3)
            self.assertEqual(n, 1)                      # stale, immediately
            t.join()
            self.assertEqual(len(b.get("/api/images")[1]["images"]), 2)
        finally:
            b.close()

    def test_fresh_rebuilds_so_that_a_file_just_delivered_is_seen(self):
        b = Bench()
        try:
            self.assertEqual(b.get("/api/images")[1]["images"], [])
            (b.web / "p91").mkdir()
            (b.web / "p91" / "new.bin").write_bytes(b"z")
            self.assertEqual(len(b.get("/api/images?fresh=1")[1]["images"]), 1)
        finally:
            b.close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SingleInstance(unittest.TestCase):
    def start(self, logs, port):
        env = dict(os.environ, BENCH_LOGS=str(logs), BENCH_PORT=str(port), BENCH_WEB=str(logs))
        return subprocess.Popen([sys.executable, str(SERVER_DIR / "bench_server.py")], env=env, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def test_one_server_per_capture_directory_and_nobody_gets_killed(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            a = self.start(d1, free_port())
            time.sleep(1.0)
            self.assertIsNone(a.poll())
            b = self.start(d1, free_port())                   # same capture directory
            out, err = b.communicate(timeout=10)
            self.assertNotEqual(b.returncode, 0)
            self.assertIn("another instance already serves", err)
            self.assertIsNone(a.poll(), "the owner must be left alone")
            c = self.start(d2, free_port())                   # another bench (other capture directory) is legitimate
            time.sleep(1.0)
            self.assertIsNone(c.poll())
            d = self.start(d2, free_port())
            d.communicate(timeout=10)
            self.assertNotEqual(d.returncode, 0)
            for p in (a, c):
                p.terminate(); p.wait(timeout=5)

    def test_a_port_owned_by_something_else_is_reported_not_taken(self):
        with tempfile.TemporaryDirectory() as d, socket.socket() as s:
            s.bind(("0.0.0.0", 0)); s.listen()
            p = self.start(d, s.getsockname()[1])
            out, err = p.communicate(timeout=10)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("cannot listen", err)


if __name__ == "__main__":
    unittest.main()

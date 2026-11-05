"""Real process/lock boundary tests. Never uses USB or existing bench state."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.journal import Journal
from tools.run_server import ServerLauncher


CHILD = r'''
import fcntl,json,os,signal,sys,time
from pathlib import Path
root=Path(sys.argv[1]);delay=float(sys.argv[2])
lock=(root/'child.lock').open('a')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
stopping=False
def stop(*_):
    global stopping
    stopping=True
signal.signal(signal.SIGTERM,stop)
def record(kind):
    with (root/'events.jsonl').open('a') as stream:
        stream.write(json.dumps({'kind':kind,'pid':os.getpid(),'at':time.monotonic()})+'\n')
record('started')
while not stopping: time.sleep(.005)
time.sleep(delay)
record('exited')
'''


class LauncherTests(unittest.TestCase):
    def test_restart_waits_for_actual_child_exit_and_keeps_exclusion(self):
        for delay in [0,.25]:
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                launcher = ServerLauncher([sys.executable,'-c',CHILD,directory,str(delay)],shutdown_timeout=2)
                errors = []

                def operator():
                    try:
                        self.wait_events(root,1)
                        launcher.request('restart')
                        self.wait_events(root,3)
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        launcher.request('stop')

                thread = threading.Thread(target=operator,daemon=True)
                thread.start()
                with contextlib.redirect_stderr(io.StringIO()) as output:
                    try:
                        self.assertEqual(launcher.run(),0)
                    finally:
                        if launcher.child and launcher.child.poll() is None:
                            launcher.child.kill();launcher.child.wait(timeout=5)
                thread.join(timeout=5)
                self.assertFalse(errors,errors)
                rows = self.wait_events(root,4)
                self.assertEqual([row['kind'] for row in rows],['started','exited','started','exited'])
                self.assertNotEqual(rows[0]['pid'],rows[2]['pid'])
                self.assertGreaterEqual(rows[2]['at'],rows[1]['at'])
                self.assertIn('no replacement starts before exit',output.getvalue())

    @staticmethod
    def wait_events(root,count):
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            path = root/'events.jsonl'
            try:
                rows = [json.loads(row) for row in path.read_text().splitlines()]
            except (FileNotFoundError,json.JSONDecodeError):
                rows = []
            if len(rows)>=count:
                return rows
            time.sleep(.01)
        raise AssertionError(f'Timed out waiting for {count} child events')

    def test_shutdown_timeout_refuses_replacement_without_killing_old_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = ServerLauncher([sys.executable,'-c',CHILD,directory,'2'],shutdown_timeout=.1)
            def operator():
                self.wait_events(root,1)
                launcher.request('restart')
            thread = threading.Thread(target=operator,daemon=True)
            thread.start()
            with contextlib.redirect_stderr(io.StringIO()) as output:
                try:
                    self.assertEqual(launcher.run(),124)
                    self.assertIsNone(launcher.child.poll())
                    self.assertEqual(len(self.wait_events(root,1)),1)
                    self.assertIn('replacement refused',output.getvalue())
                finally:
                    if launcher.child and launcher.child.poll() is None:
                        launcher.child.kill();launcher.child.wait(timeout=5)
            thread.join(timeout=5)

    def test_journal_waits_for_lock_release_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as directory:
            holder = Journal(directory)
            inode = (Path(directory)/'server.lock').stat().st_ino
            timer = threading.Timer(.2,holder.lock.close)
            timer.start()
            try:
                with contextlib.redirect_stderr(io.StringIO()) as output:
                    replacement = Journal(directory,lock_timeout=2)
                try:
                    self.assertIn('waiting up to',output.getvalue())
                    self.assertIn('lock acquired',output.getvalue())
                    self.assertEqual((Path(directory)/'server.lock').stat().st_ino,inode)
                    self.assertTrue((Path(directory)/'operations.sqlite3').exists())
                finally:
                    replacement.close()
            finally:
                timer.join();holder.close()

    def test_lock_wait_timeout_refuses_second_writer_and_retains_original_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            holder = Journal(directory)
            inode = (Path(directory)/'server.lock').stat().st_ino
            try:
                for timeout in [.1,0]:
                    with contextlib.redirect_stderr(io.StringIO()) as output:
                        with self.assertRaisesRegex(BlockingIOError,'startup refused'):
                            Journal(directory,lock_timeout=timeout)
                    self.assertIn('Lock and journal are preserved',output.getvalue())
                self.assertEqual((Path(directory)/'server.lock').stat().st_ino,inode)
                self.assertIsNotNone(holder.db.execute('SELECT 1').fetchone())
            finally:
                holder.close()

    def test_signal_handlers_restart_then_stop_real_server_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root/'tokens.json'
            config.write_text(json.dumps({'launcher-test-client-token':{'id':'test','role':'client','scopes':['read']}}))
            env = dict(os.environ,BENCH_TOKENS_FILE=str(config),BENCH_STATE_DIR=str(root/'state'),BENCH_PORT='0',PYTHONUNBUFFERED='1')
            seed = Journal(root/'state')
            seed.db.execute('CREATE TABLE retained_test_value (value TEXT)')
            seed.db.execute("INSERT INTO retained_test_value VALUES ('keep')")
            seed.db.commit();seed.close()
            command = [sys.executable,str(Path(__file__).resolve().parents[1]/'tools/run_server.py')]
            log = root/'launcher.log'
            with log.open('w') as output:
                process = subprocess.Popen(command,env=env,stdout=output,stderr=output)
            children = []
            try:
                deadline = time.monotonic()+10
                while time.monotonic()<deadline:
                    text = log.read_text()
                    children = [int(line.split('PID ')[1].split(')')[0]) for line in text.splitlines() if line.startswith('Bench server child started')]
                    if len(children)==1 and text.count('Running on')==1:
                        break
                    if process.poll() is not None:
                        self.fail(text)
                    time.sleep(.02)
                self.assertEqual(len(children),1,log.read_text())
                process.send_signal(signal.SIGHUP)
                deadline = time.monotonic()+10
                while time.monotonic()<deadline:
                    text = log.read_text()
                    children = [int(line.split('PID ')[1].split(')')[0]) for line in text.splitlines() if line.startswith('Bench server child started')]
                    if len(children)==2 and text.count('Running on')==2:
                        break
                    time.sleep(.02)
                self.assertEqual(len(children),2,text)
                self.assertNotEqual(*children)
                process.terminate()
                self.assertEqual(process.wait(timeout=10),0,log.read_text())
                self.assertTrue((root/'state/operations.sqlite3').exists())
                self.assertTrue((root/'state/server.lock').exists())
                with sqlite3.connect(root/'state/operations.sqlite3') as database:
                    self.assertEqual(database.execute('SELECT value FROM retained_test_value').fetchone(),('keep',))
            finally:
                forced_cleanup = process.poll() is None
                if forced_cleanup:
                    process.kill();process.wait(timeout=5)
                # Only children created by this test, never an existing bench.
                for pid in children if forced_cleanup else []:
                    try:
                        os.kill(pid,signal.SIGKILL)
                    except ProcessLookupError:
                        pass

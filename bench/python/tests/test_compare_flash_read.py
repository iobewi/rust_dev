"""Offline qualification of the A/B/C esptool comparison helper. No serial port."""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "compare_flash_read.py"
spec = importlib.util.spec_from_file_location("compare_flash_read", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FlashReadComparisonTests(TestCase):
    def test_dry_run_opens_no_serial_port(self):
        with patch.object(module.subprocess, "run", side_effect=AssertionError("hardware touched")):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(module.main([]), 0)
        self.assertIn("DRY RUN", output.getvalue())

    def test_sha_evidence_requires_matching_b_and_c(self):
        sha = module.EXPECTED_A_SHA
        self.assertEqual(module.compare_evidence(sha, None)["integrity_verdict"], "PENDING_IDLAB_C")
        self.assertEqual(module.compare_evidence(sha, sha)["integrity_verdict"], "PASS")
        self.assertEqual(module.compare_evidence("0" * 64, sha)["integrity_verdict"], "FAIL")
        self.assertEqual(module.compare_evidence(sha, "0" * 64)["integrity_verdict"], "FAIL")

    def test_explicit_read_only_command_and_private_evidence(self):
        with TemporaryDirectory() as d:
            folder = Path(d) / "evidence"
            data = bytes([0xA5]) * module.SIZE
            def fake_run(cmd, **kwargs):
                self.assertEqual(cmd[:3], ["python-test", "-m", "esptool"])
                self.assertIn("read-flash", cmd)
                self.assertNotIn("write-flash", cmd)
                self.assertNotIn("erase-flash", cmd)
                self.assertEqual(cmd[cmd.index("read-flash")+1:cmd.index("read-flash")+3],
                                 [hex(module.OFFSET), hex(module.SIZE)])
                Path(cmd[-1]).write_bytes(data)
                return SimpleNamespace(returncode=0, stdout="stub connected\\nRead 1048576 bytes from 0x00010000 in 3.2 seconds (2621.4 kbit/s).", stderr="")
            with patch.object(module.subprocess, "run", side_effect=fake_run):
                report = module.run_b("COM9", folder, "python-test", None)
            self.assertEqual(report["bytes_read"], module.SIZE)
            self.assertEqual(report["esptool_read_elapsed_s"], 3.2)
            self.assertEqual(report["esptool_read_bytes_per_second"], round(module.SIZE / 3.2))
            self.assertEqual(report["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(report["integrity"]["integrity_verdict"], "FAIL")
            self.assertTrue((folder / "report-B.json").exists())
            self.assertTrue((folder / "esptool-B.log").exists())

    def test_nonempty_evidence_directory_refused_without_touching_hardware(self):
        with TemporaryDirectory() as d:
            folder = Path(d)
            (folder / "old-result.bin").write_bytes(b"x")
            with patch.object(module.subprocess, "run", side_effect=AssertionError("hardware touched")):
                with self.assertRaises(FileExistsError):
                    module.run_b("COM9", folder, "python-test", None)

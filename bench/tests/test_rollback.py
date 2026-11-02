"""The rollback verifier on synthetic captures and read-backs. No board."""
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from test_canonical import SIZES, write
from test_p5c import frame, otm1

spec = importlib.util.spec_from_file_location("check_rollback", Path(__file__).resolve().parent.parent / "rollback/check_rollback.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
M = R.CC


def log(t, line):
    h, rest = divmod(t, 3600); m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f} [uart] {line}\n"


def capture(reset_after=15.3, extra=""):
    t = 36000.0
    out = [log(t, "[bench] MARK X"), log(t + 1, "INFO - ota: activating d; reboot requested"), log(t + 1.3, "boot authentication: VERIFIED"), log(t + 1.4, "boot: slots checked, microseconds 0x00067c00"),
           log(t + 1.7, "boot: plan slot=0x00000000 seq=0x00000003"), log(t + 1.7, "boot: otadata write sector=0x00000000 seq=0x00000003 state=0x00000001"),
           log(t + 1.7, "boot: image ok, segments=0x6 entry=0x1"), log(t + 1.7, "boot: jump 0x4037bc7c"), log(t + 2.0, "INFO - boot: running ota_0 status PendingVerify"),
           log(t + 2.0, "INFO - rollback test image: self-check hangs on purpose, this image never confirms")]
    r = t + 2.0 + reset_after
    out += [log(r, "rst:0x3 (RTC_SW_SYS_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)"), log(r + 0.3, "boot authentication: VERIFIED"), log(r + 0.7, "boot: slots checked, microseconds 0x00067c00"),
            log(r + 1.0, "boot: plan slot=0x00000001 seq=0x00000002"), log(r + 1.0, "boot: otadata write sector=0x00000000 seq=0x00000003 state=0x00000004"),
            log(r + 1.0, "boot: image ok, segments=0x6 entry=0x1"), log(r + 1.0, "boot: jump 0x4037bc7c"), log(r + 1.3, "INFO - boot: running ota_1 status Stable"),
            log(r + 1.3, "INFO - boot: stable"), log(r + 11, "INFO - OTA ready: HTTPS :443")]
    return "".join(out) + extra


def run_capture(text, stage="rollback"):
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "c.txt"; f.write_text(text)
        M.fails.clear()
        with redirect_stdout(io.StringIO()):
            R.capture(type("A", (), {"file": str(f), "stage": stage, "after": None})())
        return list(M.fails)


class Capture(unittest.TestCase):
    def test_a_real_rollback_passes(self):
        self.assertEqual(run_capture(capture()), [])

    def test_a_confirmation_fails(self):
        self.assertTrue(run_capture(capture().replace("INFO - boot: running ota_0 status PendingVerify", "INFO - boot: running ota_0 status PendingVerify\n00:00:00.000 [uart] INFO - boot: confirmed")))

    def test_a_reset_too_early_or_too_late_fails(self):
        self.assertTrue(run_capture(capture(reset_after=3.0)))
        self.assertTrue(run_capture(capture(reset_after=40.0)))

    def test_a_hardware_watchdog_reset_is_not_the_software_path(self):
        self.assertTrue(run_capture(capture().replace("rst:0x3 (RTC_SW_SYS_RST)", "rst:0x7 (TG0WDT_SYS_RST)")))

    def test_no_trace_means_it_was_not_d(self):
        self.assertTrue(run_capture(capture().replace("INFO - rollback test image: self-check hangs on purpose, this image never confirms\n", "")))

    def test_d_started_again_after_the_rollback_fails(self):
        self.assertTrue(run_capture(capture(extra=log(36100, "INFO - boot: running ota_0 status PendingVerify"))))

    def test_a_missing_aborted_write_fails(self):
        self.assertTrue(run_capture(capture().replace("state=0x00000004", "state=0x00000003")))


class State(unittest.TestCase):
    IMG = b"D" * 70000
    PROOF = b"P" * 4096

    def run_state(self, od0=None, meta_after_seq=8, stage=0, patch=(), adig="sha256:aa"):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "img.bin"; f.write_bytes(self.IMG)
            g = Path(d) / "proof.bin"; g.write_bytes(self.PROOF)
            span = -(-len(self.IMG) // 65536) * 65536
            before = [(0x10000, b"\xff" * 0x1000 + (M.P5C.encode_entry(1, 2) + b"\xff" * 4064) + (M.P5C.encode_entry(2, 2) + b"\xff" * 4064) +
                       frame(5, otm1(0, adig="sha256:aa", adep="p5")) + frame(4, otm1(0, adig="sha256:aa", adep="p5")))]
            after = [(0x1D0000 - 0x1D0000 + 0x20000, self.IMG), (0x20000 + len(self.IMG), b"\xff" * (span - len(self.IMG))), (0x1CF000, self.PROOF),
                     (0x10000, b"\xff" * 0x1000 + (M.P5C.encode_entry(3, 4) + b"\xff" * 4064) + (M.P5C.encode_entry(2, 2) + b"\xff" * 4064) +
                      frame(meta_after_seq, otm1(stage, adig=adig, adep="p5")) + frame(meta_after_seq - 1, otm1(2, "ota_0", "sha256:dd", "d", 5, "sha256:aa", "p5")))] + list(patch)
            write(d, "b", before); write(d, "a", after)
            M.fails.clear()
            with redirect_stdout(io.StringIO()):
                R.state(type("A", (), {"dumps": d, "before": "b", "after": "a", "image": str(f), "proof": str(g)})())
            return list(M.fails)

    def test_the_expected_state_after_a_rollback_passes(self):
        self.assertEqual(self.run_state(), [])

    def test_wrong_meta_sequence_or_staged_residue_or_promoted_d_fail(self):
        self.assertTrue(self.run_state(meta_after_seq=7))
        self.assertTrue(self.run_state(stage=2))
        self.assertTrue(self.run_state(adig="sha256:other"))

    def test_a_touched_b_entry_or_zone_fails(self):
        self.assertTrue(self.run_state(patch=[(0x12000, b"\x00")]))
        self.assertTrue(self.run_state(patch=[(0x37F000, b"x")]))


if __name__ == "__main__":
    unittest.main()

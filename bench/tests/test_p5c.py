"""The P5-C verifier on files: codec against bytes that were on the board, and the capture rules on synthetic captures. No board."""
import importlib.util
import io
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_p5c", Path(__file__).resolve().parent.parent / "p5c/check_p5c.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

S0_SECTOR0_HEAD = "01000000424f4f5402000000c829c3fdffffffff3ca5c35a020000009a984347"      # otadata sector 0 read from the board (seq 1, Valid)


def frame(seq, payload):
    body = b"OTSF" + bytes([1, 0]) + struct.pack("<HI", len(payload), seq) + payload + b"\0" * (-len(payload) % 4)
    return (body + struct.pack("<I", zlib.crc32(body))).ljust(4096, b"\xff")


def otm1(stage, slot="", digest="", dep="", size=0, adig="", adep=""):
    f = [slot, digest, dep, adig, adep]
    return b"OTM1" + bytes([stage]) + struct.pack("<I", size) + struct.pack("<5H", *[len(x) for x in f]) + "".join(f).encode()


def meta(copy0, copy1, sectors=(b"", b"")):
    od = [(e + b"\xff" * 4096)[:4096] for e in sectors]
    return b"\xff" * 0x1000 + od[0] + od[1] + copy0 + copy1


class Codec(unittest.TestCase):
    def test_entry_encoding_is_the_one_the_board_holds(self):
        self.assertEqual(M.encode_entry(1, M.VALID)[:32].hex(), S0_SECTOR0_HEAD)

    def test_decode_distinguishes_blank_corrupt_and_states(self):
        for st in (M.NEW, M.PENDING, M.VALID, M.INVALID, M.ABORTED):
            self.assertEqual(M.decode_entry(M.encode_entry(2, st) + b"\xff" * 4064), (2, st))
        self.assertEqual(M.decode_entry(b"\xff" * 4096), "blank")
        torn = bytearray(M.encode_entry(2, M.NEW)); torn[20:24] = b"\xff" * 4                  # body written, commit word not
        self.assertEqual(M.decode_entry(bytes(torn)), "corrupt")

    def test_ota_meta_final_state_is_read_back_as_expected(self):
        dig = "sha256:" + "ab" * 32
        before = otm1(1, "ota_1", dig, "dep", 1050112)
        after = otm1(0, adig=dig, adep="dep")
        m, frames = M.meta_of(meta(frame(5, after), frame(4, otm1(2, "ota_1", dig, "dep", 1050112, dig, "dep"))))
        self.assertEqual((m["copy"], m["seq"], m["stage"], m["active_digest"], m["active_deployment_id"]), (0, 5, "none", dig, "dep"))
        self.assertEqual(frames[1][0], 4)
        m, _ = M.meta_of(meta(frame(1, otm1(0)), frame(2, before)))
        self.assertEqual((m["copy"], m["seq"], m["stage"], m["slot"], m["size"]), (1, 2, "written", "ota_1", 1050112))

    def test_a_torn_frame_is_not_a_record(self):
        bad = bytearray(frame(7, otm1(0))); bad[20] ^= 1
        m, frames = M.meta_of(meta(bytes(bad), frame(6, otm1(0))))
        self.assertEqual((m["seq"], frames[0]), (6, "invalid"))


class Capture(unittest.TestCase):
    A1B1 = """ota: activating p5-b2-20261101; reboot requested
boot authentication: VERIFIED
boot: slots checked, microseconds 0x00067f10
boot: plan slot=0x00000001 seq=0x00000002
boot: otadata write sector=0x00000001 seq=0x00000002 state=0x00000001
boot: image ok, segments=0x00000006 entry=0x4037bc7c
boot: jump 0x4037bc7c
INFO - boot: running ota_1 status PendingVerify
INFO - boot: confirmed
INFO - OTA ready: HTTPS :443; upload then activate by deployment id
"""

    def run_stage(self, text, stage):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.txt"; f.write_text(text)
            M.fails.clear()
            with redirect_stdout(io.StringIO()):
                M.capture(type("A", (), {"file": str(f), "stage": stage, "after": None})())
            return list(M.fails)

    def test_activation_and_first_boot_of_b_pass(self):
        self.assertEqual(self.run_stage(self.A1B1, "a1b1"), [])

    def test_missing_confirmation_fails(self):
        self.assertTrue(self.run_stage(self.A1B1.replace("INFO - boot: confirmed\n", ""), "a1b1"))

    def test_rollback_to_a_fails(self):
        self.assertTrue(self.run_stage(self.A1B1.replace("ota_1 status PendingVerify", "ota_0 status Stable"), "a1b1"))

    def test_a_signature_failure_line_fails(self):
        self.assertTrue(self.run_stage("Signature Check Failed\n" + self.A1B1, "a1b1"))

    def test_steps_out_of_order_fail(self):
        lines = self.A1B1.splitlines(); lines[7], lines[8] = lines[8], lines[7]
        self.assertTrue(self.run_stage("\n".join(lines) + "\n", "a1b1"))

    def test_persistence_boot_must_not_write_otadata(self):
        r1 = """boot authentication: VERIFIED
boot: slots checked, microseconds 0x00033000
boot: plan slot=0x00000001 seq=0x00000002
boot: image ok, segments=0x00000006 entry=0x4037bc7c
boot: jump 0x4037bc7c
INFO - boot: running ota_1 status Stable
INFO - boot: stable
INFO - OTA ready: HTTPS :443; upload then activate by deployment id
"""
        self.assertEqual(self.run_stage(r1, "r1"), [])
        self.assertTrue(self.run_stage(r1.replace("boot: image ok", "boot: otadata write sector=0x1 seq=2\nboot: image ok"), "r1"))


if __name__ == "__main__":
    unittest.main()

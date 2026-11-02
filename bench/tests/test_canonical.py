"""The checkpoint diff of the canonical-length qualification, on synthetic read-backs. No board."""
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_canonical", Path(__file__).resolve().parent.parent / "canonical/check_canonical.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
SIZES = {"boot-0x0": 0xA000, "gap-0xa000": 0x6000, "meta-0x10000": 0x5000, "ota0-0x20000": 0x1B0000, "ota1-0x1d0000": 0x1B0000, "cfg-0x380000": 0x80000}


def write(d, tag, patch=None):
    for suffix, base in M.FILES:
        b = bytearray(b"\xaa" * SIZES[suffix])
        for (addr, data) in (patch or []):
            if base <= addr < base + len(b):
                b[addr - base:addr - base + len(data)] = data
        (Path(d) / f"{tag}_{suffix}.bin").write_bytes(bytes(b))


def run(d, allow):
    M.fails.clear()
    with redirect_stdout(io.StringIO()):
        M.diff(type("A", (), {"dumps": d, "before": "a", "after": "b", "allow": allow})())
    return list(M.fails)


class Diff(unittest.TestCase):
    def test_a_change_inside_the_allowed_zone_passes(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "a"); write(d, "b", [(0x1D0000, b"junk"), (0x37F000, b"proof")])
            self.assertEqual(run(d, "ota1_image,ota1_proof"), [])

    def test_a_change_outside_the_allowed_zones_fails_and_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "a"); write(d, "b", [(0x1D0000, b"junk"), (0x13000, b"\x00")])           # ota_meta touched as well
            self.assertEqual(run(d, "ota1_image"), ["ota_meta unchanged"])

    def test_the_proof_sector_is_a_zone_of_its_own(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "a"); write(d, "b", [(0x1CF000, b"\x00")])                                # last sector of ota_0: its proof, not its image
            self.assertEqual(run(d, "ota0_image"), ["ota0_proof unchanged"])

    def test_nothing_changed_passes_with_no_allowance(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "a"); write(d, "b")
            self.assertEqual(run(d, ""), [])

    def test_a_missing_checkpoint_fails(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "a")
            self.assertTrue(run(d, ""))


class Capture(unittest.TestCase):
    WRITING = "INFO - ota: writing p6-n1-c (1050128 bytes) to ota_1\n"

    def run_stage(self, text, stage):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.txt"; f.write_text(text)
            M.fails.clear()
            with redirect_stdout(io.StringIO()):
                M.capture(type("A", (), {"file": str(f), "stage": stage, "after": None})())
            return list(M.fails)

    def test_a_refused_upload_writes_but_never_publishes(self):
        self.assertEqual(self.run_stage(self.WRITING, "refused"), [])
        self.assertTrue(self.run_stage(self.WRITING + "INFO - ota: flash digest and image verified; publishing x\n", "refused"))

    def test_a_refused_upload_that_restarted_the_board_fails(self):
        self.assertTrue(self.run_stage(self.WRITING + "boot authentication: VERIFIED\n", "refused"))
        self.assertTrue(self.run_stage("Signature Check Failed\n" + self.WRITING, "refused"))

    def test_an_upload_that_was_not_even_written_fails(self):
        self.assertTrue(self.run_stage("nothing\n", "refused"))

    def test_an_accepted_upload_reaches_publication(self):
        self.assertEqual(self.run_stage(self.WRITING + "INFO - ota: flash digest and image verified; publishing x\n", "accepted"), [])
        self.assertTrue(self.run_stage(self.WRITING, "accepted"))


if __name__ == "__main__":
    unittest.main()

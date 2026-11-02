"""The checkpoint diff of the canonical-length qualification, on synthetic read-backs. No board."""
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from test_p5c import frame, meta, otm1          # OTSF frame, OTM1 record and ota_meta image builders of the P5-C tests

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


class Restore(unittest.TestCase):
    def plan(self, d, out):
        M.fails.clear()
        buf = io.StringIO()
        with redirect_stdout(buf):
            M.plan_restore(type("A", (), {"dumps": d, "current": "cur", "reference": "ref", "out": out})())
        return list(M.fails), buf.getvalue()

    def test_only_the_differing_sectors_are_planned_and_coalesced(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            write(d, "ref"); write(d, "cur", [(0x2D0000, b"\x00" * 16), (0x37F000, b"blank"), (0x13000, b"x"), (0x1D0000, b"y"), (0x1D1000, b"z")])
            fails, text = self.plan(d, out)
            self.assertEqual(fails, [])
            names = sorted(p.name for p in Path(out).iterdir())
            self.assertEqual(names, ["p6r-ref-0x13000-1000.bin", "p6r-ref-0x1d0000-2000.bin", "p6r-ref-0x2d0000-1000.bin", "p6r-ref-0x37f000-1000.bin"])
            self.assertEqual((Path(out) / "p6r-ref-0x37f000-1000.bin").read_bytes(), b"\xaa" * 4096)          # the reference bytes, not the current ones

    def test_a_difference_outside_the_restorable_zones_stops_the_plan(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            write(d, "ref"); write(d, "cur", [(0x0, b"boot"), (0x37F000, b"blank")])
            fails, _ = self.plan(d, out)
            self.assertEqual(len(fails), 1)
            self.assertEqual(list(Path(out).iterdir()), [])

    def test_a_conforming_state_plans_nothing_and_a_partial_restore_only_shrinks_the_plan(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            write(d, "ref"); write(d, "cur")
            self.assertEqual(self.plan(d, out)[0], [])
            self.assertEqual(list(Path(out).iterdir()), [])
            write(d, "cur", [(0x37F000, b"a"), (0x2D0000, b"b")])
            self.assertEqual(len(list(Path(out).iterdir())), 0)
            self.plan(d, out); n1 = len(list(Path(out).iterdir()))
            write(d, "cur", [(0x2D0000, b"b")])                                        # the proof sector was restored, the other not yet
            for f in Path(out).iterdir(): f.unlink()
            self.plan(d, out); n2 = len(list(Path(out).iterdir()))
            self.assertEqual((n1, n2), (2, 1))


class Refused(unittest.TestCase):
    def run_state(self, patch_after, img=b"IMG" * 30000):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "img.bin"; f.write_bytes(img)
            span = -(-len(img) // 65536) * 65536
            ota1_after = [(0x1D0000, img), (0x1D0000 + len(img), b"\xff" * (span - len(img))), (0x37F000, b"\xff" * 4096)]
            write(d, "b"); write(d, "a", ota1_after + patch_after)
            M.fails.clear()
            with redirect_stdout(io.StringIO()):
                M.refused_state(type("A", (), {"dumps": d, "before": "b", "after": "a", "image": str(f)})())
            return list(M.fails)

    def test_the_expected_state_after_a_refusal_passes(self):
        self.assertEqual(self.run_state([]), [])

    def test_a_published_proof_or_a_touched_ota_meta_fails(self):
        self.assertTrue(self.run_state([(0x37F000, b"IAP1")]))
        self.assertEqual(self.run_state([(0x13000, b"\x00")]), ["ota_meta unchanged"])

    def test_bytes_that_are_not_the_received_ones_fail(self):
        self.assertTrue(self.run_state([(0x1D0010, b"\x00")]))


class Accepted(unittest.TestCase):
    IMG = b"IMG" * 30000
    PROOF = b"P" * 4096

    def meta_with(self, seq, stage, slot, size, digest, dep):
        return meta(frame(seq, otm1(stage, slot, digest, dep, size)), frame(seq - 1, otm1(0)))

    def run_state(self, patch_after, id_="p6-b", seq_after=6, digest=None, img=None):
        import hashlib
        img = img or self.IMG
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "img.bin"; f.write_bytes(img)
            g = Path(d) / "proof.bin"; g.write_bytes(self.PROOF)
            span = -(-len(img) // 65536) * 65536
            dig = digest or "sha256:" + hashlib.sha256(img).hexdigest()
            write(d, "b", [(0x10000, self.meta_with(5, 0, "", 0, "", ""))])
            write(d, "a", [(0x1D0000, img), (0x1D0000 + len(img), b"\xff" * (span - len(img))), (0x37F000, self.PROOF),
                           (0x10000, self.meta_with(seq_after, 1, "ota_1", len(img), dig, id_))] + patch_after)
            M.fails.clear()
            with redirect_stdout(io.StringIO()):
                M.accepted_state(type("A", (), {"dumps": d, "before": "b", "after": "a", "image": str(f), "proof": str(g), "slot": "ota_1", "id": "p6-b"})())
            return list(M.fails)

    def test_the_expected_state_after_an_accepted_upload_passes(self):
        self.assertEqual(self.run_state([]), [])

    def test_a_wrong_proof_digest_or_sequence_or_stray_change_fails(self):
        self.assertTrue(self.run_state([(0x37F000, b"X")]))
        self.assertTrue(self.run_state([], digest="sha256:" + "00" * 32))
        self.assertTrue(self.run_state([], seq_after=7))
        self.assertTrue(self.run_state([(0x11000, b"\x00")]))
        self.assertTrue(self.run_state([], id_="other"))


if __name__ == "__main__":
    unittest.main()

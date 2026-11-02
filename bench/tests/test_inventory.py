"""What /api/images lists, how it is categorised and counted. Nothing is deduplicated, nothing is moved."""
import json
import unittest

from serverlib import Bench


class Listing(unittest.TestCase):
    def setUp(self):
        self.b = Bench()
        w = self.b.web
        def put(rel, data, root=w):
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        self.put = put
        put("p91/art.bin", b"SAME")                          # an artifact...
        put("p91/backups/bk.bin", b"BACKUP")
        put("p91/archive/old.bin", b"OLD")
        put("c33/legacy.bin", b"LEGACY")
        put("loose.bin", b"LOOSE")
        put("p91/ok.json", json.dumps({"name": "n", "builds": [{"chipFamily": "ESP32-S3", "parts": [{"path": "p91/art.bin", "offset": 0}]}]}).encode())
        put("c33/firmware-layout.json", b'{"regions": []}')
        put("p91/broken.json", b"{not json")
        put("cap.bin", b"SAME", root=self.b.logs / "dumps")      # ... and a read-back with the same content
        put("archive/cap-20261101.bin", b"SAME", root=self.b.logs / "dumps")

    def tearDown(self):
        self.b.close()

    def paths(self, q=""):
        return {i["path"]: i for i in self.b.get("/api/images" + q)[1]["images"]}

    def test_default_is_the_allowed_folders_and_current_captures_without_archives(self):
        self.assertEqual(set(self.paths()), {"p91/art.bin", "p91/backups/bk.bin", "dumps/cap.bin"})

    def test_archives_and_other_folders_are_opt_in(self):
        self.assertEqual(set(self.paths("?archives=1")), {"p91/art.bin", "p91/backups/bk.bin", "p91/archive/old.bin", "dumps/cap.bin", "dumps/archive/cap-20261101.bin"})
        self.assertEqual(set(self.paths("?scope=all")), {"p91/art.bin", "p91/backups/bk.bin", "c33/legacy.bin", "loose.bin", "dumps/cap.bin"})
        self.assertEqual(len(self.paths("?scope=all&archives=1")), 7)

    def test_categories_origin_and_kept_metadata(self):
        p = self.paths("?scope=all&archives=1")
        cats = {k: v["category"] for k, v in p.items()}
        self.assertEqual(cats, {"p91/art.bin": "artifact", "p91/backups/bk.bin": "backup", "p91/archive/old.bin": "archive", "c33/legacy.bin": "artifact",
                                "loose.bin": "artifact", "dumps/cap.bin": "capture", "dumps/archive/cap-20261101.bin": "archive"})
        a = p["p91/art.bin"]
        self.assertEqual((a["size"], a["origin"], a["source"], a["allowed"]), (4, "p91", "web", True))
        self.assertEqual(len(a["sha256"]), 64)
        self.assertTrue(isinstance(a["mtime"], int))
        self.assertFalse(p["c33/legacy.bin"]["allowed"])

    def test_files_and_unique_digests_are_counted_apart_and_never_merged(self):
        r = self.b.get("/api/images")[1]
        self.assertEqual(r["counts"]["files"], 3)
        self.assertEqual(r["counts"]["unique_sha256"], 2)
        self.assertEqual(r["counts"]["duplicate_groups"], 0 + 1)
        same = [i for i in r["images"] if i["sha256"] == self.paths()["p91/art.bin"]["sha256"]]
        self.assertEqual(sorted(i["path"] for i in same), ["dumps/cap.bin", "p91/art.bin"])    # same digest, two entries
        self.assertEqual(r["counts"]["by_category"], {"artifact": 1, "backup": 1, "capture": 1})

    def test_excluded_files_are_counted_but_not_hashed(self):
        hashed = []
        real = self.b.mod.inventory.sha256_of
        self.b.mod.inventory.sha256_of = lambda p: (hashed.append(p.name), real(p))[1]
        r = self.b.get("/api/images?fresh=1")[1]
        self.assertEqual(r["excluded"], {"archives": 2, "outside_allowed": 2})
        self.assertEqual(sorted(hashed), ["art.bin", "bk.bin", "cap.bin"])

    def test_json_that_is_not_esp_web_tools_is_said_so(self):
        r = self.b.get("/api/images?scope=all")[1]
        self.assertEqual([m["path"] for m in r["manifests"]], ["p91/ok.json"])
        self.assertEqual({j["path"]: j["reason"] for j in r["ignored_json"]},
                         {"p91/broken.json": "invalid-json", "c33/firmware-layout.json": "not-esp-web-tools (no builds[0].parts)"})

    def test_nothing_on_disk_is_touched(self):
        before = sorted(str(p.relative_to(self.b.web)) for p in self.b.web.rglob("*"))
        self.b.get("/api/images?scope=all&archives=1&fresh=1")
        self.assertEqual(sorted(str(p.relative_to(self.b.web)) for p in self.b.web.rglob("*")), before)


class Configuration(unittest.TestCase):
    def test_allowed_folders_come_from_the_environment(self):
        b = Bench(BENCH_INVENTORY_DIRS="c33,p91")
        try:
            (b.web / "c33").mkdir(); (b.web / "c33" / "x.bin").write_bytes(b"1")
            self.assertEqual([i["path"] for i in b.get("/api/images")[1]["images"]], ["c33/x.bin"])
            self.assertEqual(b.get("/api/images")[1]["scope"]["allowed_dirs"], ["c33", "p91"])
        finally:
            b.close()


class Lookup(unittest.TestCase):
    """`path=` narrows the answer to the named files: a write checks its own deliverables, not the whole listing."""
    def setUp(self):
        self.b = Bench()
        for i in range(400):                                   # a listing far beyond 64 KiB once serialized
            f = self.b.logs / "dumps" / "archive" / f"bulk-{i:04d}-0x1d0000-20261101-000000.bin"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(bytes([i % 251]) * 16)
        (self.b.web / "p91").mkdir()
        (self.b.web / "p91" / "art.bin").write_bytes(b"ART")

    def tearDown(self):
        self.b.close()

    def test_the_full_listing_is_beyond_64_kib_and_the_lookup_is_not(self):
        q = "/api/images?scope=all&archives=1&fresh=1"
        full = self.b.get(q)[1]
        self.assertGreater(len(json.dumps(full)), 65536)
        code, one = self.b.get(q + "&path=p91/art.bin")
        self.assertEqual(code, 200)
        self.assertEqual([i["path"] for i in one["images"]], ["p91/art.bin"])
        self.assertLess(len(json.dumps(one)), 2000)

    def test_several_paths_unknown_path_and_unchanged_default(self):
        q = "/api/images?scope=all&archives=1&fresh=1"
        _, two = self.b.get(q + "&path=p91/art.bin&path=dumps/archive/bulk-0003-0x1d0000-20261101-000000.bin")
        self.assertEqual(len(two["images"]), 2)
        self.assertEqual(self.b.get(q + "&path=nope.bin")[1]["images"], [])
        self.assertEqual(len(self.b.get(q)[1]["images"]), 401)    # no `path`: the listing as before
        self.assertEqual(self.b.get(q + "&path=p91/art.bin")[1]["images"][0]["sha256"],
                         next(i for i in self.b.get(q)[1]["images"] if i["path"] == "p91/art.bin")["sha256"])


if __name__ == "__main__":
    unittest.main()

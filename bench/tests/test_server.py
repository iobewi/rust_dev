"""The bench server over real HTTP, with a scripted 'page'. No board."""
import json
import socket
import time
import unittest

from serverlib import Bench


class Protocol(unittest.TestCase):
    def setUp(self):
        self.b = Bench()

    def tearDown(self):
        self.b.close()

    def page_runs(self, page, body, state="completed", ok=True):
        code, cmd = self.b.get("/api/cmd?wait=2&page=" + page)
        self.assertTrue(cmd.get("op"), cmd)
        self.assertEqual(self.b.post("/api/ack", {"req": cmd["req"], "claim": cmd["claim"], "page": page})[0], 200)
        return cmd

    def test_nominal_and_result_outlives_the_poller(self):
        self.assertEqual(self.b.post("/api/cmd", {"op": "mark", "req": "a", "text": "x"})[0], 200)
        cmd = self.page_runs("P1", None)
        self.b.post("/api/result", {"req": "a", "op": "mark", "ok": True, "claim": cmd["claim"]})
        for _ in range(3):                                     # asked again and again, by clients that come and go
            self.assertEqual(self.b.get("/api/result?req=a")[1]["state"], "completed")
        self.assertEqual(self.b.get("/api/status?req=a")[1]["state"], "completed")

    def test_duplicate_req_is_409_and_unknown_result_is_empty(self):
        self.b.post("/api/cmd", {"op": "mark", "req": "a"})
        self.assertEqual(self.b.post("/api/cmd", {"op": "mark", "req": "a"})[0], 409)
        self.assertEqual(self.b.get("/api/result?req=zz")[1], {})

    def test_3_a_http_client_that_leaves_the_long_poll_takes_nothing(self):
        s = socket.create_connection(("127.0.0.1", self.b.port))
        s.sendall(b"GET /api/cmd?wait=5&page=GONE HTTP/1.1\r\nHost: x\r\n\r\n")
        time.sleep(0.3)
        s.close()                                              # the reloaded page's old poll
        time.sleep(0.2)
        self.b.post("/api/cmd", {"op": "write", "req": "w"})
        time.sleep(1.2)                                        # longer than the server's liveness check
        self.assertEqual(self.b.get("/api/status?req=w")[1]["state"], "queued")

    def test_5_6_page_reload_during_execution_is_indeterminate_and_not_replayed(self):
        self.b.post("/api/cmd", {"op": "write", "req": "w"})
        self.page_runs("OLD", None)                            # acknowledged, running, then the tab is reloaded
        self.b.post("/api/cmd", {"op": "mark", "req": "m"})
        code, nxt = self.b.get("/api/cmd?wait=2&page=NEW")
        self.assertEqual(nxt["req"], "m")
        st = self.b.get("/api/status?req=w")[1]
        self.assertEqual((st["state"], st["executed"]), ("indeterminate", "started"))
        self.assertEqual(self.b.get("/api/result?req=w")[1]["state"], "indeterminate")
        self.assertEqual(self.b.get("/api/result?req=w")[1]["ok"], False)

    def test_8_expired_command_is_not_delivered_and_says_so(self):
        self.b.post("/api/cmd", {"op": "write", "req": "w", "ttl": 0.2})
        time.sleep(0.4)
        self.assertEqual(self.b.get("/api/cmd?wait=0&page=P")[1], {})
        r = self.b.get("/api/result?req=w")[1]
        self.assertEqual((r["state"], r["executed"], r["ok"]), ("expired", False, False))

    def test_unacknowledged_claim_cannot_start_late(self):
        self.b.close()
        self.b = Bench(BENCH_ACK_TIMEOUT=0.2)
        self.b.post("/api/cmd", {"op": "write", "req": "w"})
        cmd = self.b.get("/api/cmd?wait=1&page=P")[1]
        time.sleep(0.5)
        code, out = self.b.post("/api/ack", {"req": "w", "claim": cmd["claim"], "page": "P"})
        self.assertEqual((code, out["ok"]), (409, False))

    def test_a_page_without_the_acknowledgement_protocol_is_given_nothing(self):
        self.b.post("/api/cmd", {"op": "write", "req": "w"})
        t0 = time.monotonic()
        self.assertEqual(self.b.get("/api/cmd?wait=1")[1], {})          # old page: no `page` parameter
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)              # it waits like a long poll, no busy loop
        self.assertEqual(self.b.get("/api/status?req=w")[1]["state"], "queued")

    def test_commands_log_has_states_and_no_payload(self):
        self.b.post("/api/cmd", {"op": "tx", "req": "t", "hex": "deadbeef", "body": "token=SECRET"})
        cmd = self.page_runs("P", None)
        self.b.post("/api/result", {"req": "t", "ok": True})
        text = (self.b.logs / "commands.jsonl").read_text()
        self.assertNotIn("deadbeef", text)
        self.assertNotIn("SECRET", text)
        self.assertEqual([json.loads(l)["event"] for l in text.splitlines()], ["queued", "claimed", "running", "completed"])


if __name__ == "__main__":
    unittest.main()

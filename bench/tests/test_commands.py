"""Command lifecycle, no HTTP and no board: a fake clock drives the expiry."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from commands import Commands  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(**kw):
    clock = Clock()
    return Commands(ttl=kw.pop("ttl", 60), ack_timeout=kw.pop("ack_timeout", 10), clock=clock, wall=clock, **kw), clock


class Lifecycle(unittest.TestCase):
    def test_nominal(self):
        c, _ = make()
        self.assertEqual(c.enqueue({"op": "write", "req": "a"})[0], 200)
        cmd = c.claim("P1")
        self.assertEqual((cmd["req"], c.status("a")["state"]), ("a", "claimed"))
        self.assertEqual(c.ack("a", cmd["claim"], "P1")[0], 200)
        self.assertEqual(c.status("a")["state"], "running")
        self.assertEqual(c.finish({"req": "a", "ok": True})[0], 200)
        self.assertEqual((c.status("a")["state"], c.result("a")["state"], c.result("a")["ok"]), ("completed", "completed", True))

    def test_duplicates_are_refused(self):
        c, _ = make()
        c.enqueue({"op": "mark", "req": "a"})
        self.assertEqual(c.enqueue({"op": "mark", "req": "a"})[0], 409)

    def test_7_two_concurrent_commands_one_in_flight(self):
        c, _ = make()
        c.enqueue({"op": "write", "req": "a"}); c.enqueue({"op": "dump", "req": "b"})
        a = c.claim("P")
        self.assertIsNone(c.claim("P"))                          # b waits while a is claimed
        c.ack("a", a["claim"], "P")
        self.assertIsNone(c.claim("P"))                          # ... and while a runs
        c.finish({"req": "a", "ok": True})
        self.assertEqual(c.claim("P")["req"], "b")

    def test_8_expired_command_in_queue_is_never_delivered(self):
        c, clock = make(ttl=30)
        c.enqueue({"op": "write", "req": "a"})
        clock.t += 31                                            # the port came back 31 s later
        self.assertIsNone(c.claim("P"))
        st = c.status("a")
        self.assertEqual((st["state"], st["executed"]), ("expired", False))
        self.assertEqual(c.result("a")["executed"], False)       # the sender learns it did not run

    def test_default_ttl_applies_without_expires(self):
        c, clock = make(ttl=5)
        c.enqueue({"op": "reset"})
        clock.t += 6
        self.assertIsNone(c.claim("P"))

    def test_legacy_expires_deadline_still_honoured(self):
        c, clock = make()
        c.enqueue({"op": "write", "req": "a", "expires": (clock.t - 1) * 1000})
        self.assertIsNone(c.claim("P"))
        self.assertEqual(c.status("a")["state"], "expired")

    def test_claimed_without_ack_expires_and_is_not_replayed(self):
        c, clock = make(ack_timeout=10)
        c.enqueue({"op": "write", "req": "a"})
        cmd = c.claim("P")
        clock.t += 11                                            # the response never reached the page
        self.assertIsNone(c.claim("P"))
        self.assertEqual(c.ack("a", cmd["claim"], "P")[0], 409)  # a late page must not start it
        st = c.status("a")
        self.assertEqual((st["state"], st["executed"], st["reason"]), ("expired", False, "claimed-not-acknowledged"))

    def test_ack_is_valid_once_and_for_its_claim(self):
        c, _ = make()
        c.enqueue({"op": "write", "req": "a"})
        cmd = c.claim("P")
        self.assertEqual(c.ack("a", "wrong", "P")[0], 409)
        self.assertEqual(c.ack("a", cmd["claim"], "P")[0], 200)
        self.assertEqual(c.ack("a", cmd["claim"], "P")[0], 409)  # no double start

    def test_release_puts_a_never_started_command_back(self):
        c, _ = make()
        c.enqueue({"op": "write", "req": "a"})
        first = c.claim("P")
        c.release("a", first["claim"])
        second = c.claim("P")
        self.assertNotEqual(first["claim"], second["claim"])
        self.assertEqual(c.ack("a", first["claim"], "P")[0], 409)   # the old claim is dead
        self.assertEqual(c.ack("a", second["claim"], "P")[0], 200)

    def test_6_page_reload_during_a_write_is_indeterminate_not_replayed(self):
        c, _ = make()
        c.enqueue({"op": "write", "req": "w"}); c.enqueue({"op": "console", "req": "n"})
        w = c.claim("OLD"); c.ack("w", w["claim"], "OLD")
        nxt = c.claim("NEW")                                     # the reloaded page polls
        st = c.status("w")
        self.assertEqual((st["state"], st["executed"], st["reason"]), ("indeterminate", "started", "page-replaced-during-execution"))
        self.assertEqual(c.result("w")["state"], "indeterminate")
        self.assertEqual(nxt["req"], "n")                         # the queue goes on; `w` is not re-delivered
        self.assertEqual(c.status("w")["state"], "indeterminate")

    def test_9_late_result_after_the_verdict_is_kept_but_changes_nothing(self):
        c, _ = make()
        c.enqueue({"op": "write", "req": "w"})
        w = c.claim("OLD"); c.ack("w", w["claim"], "OLD")
        c.claim("NEW")
        code, out = c.finish({"req": "w", "ok": True})
        self.assertEqual((code, out["late"]), (200, True))
        self.assertEqual(c.result("w")["state"], "indeterminate")
        self.assertEqual(c.status("w")["late_result"]["state"], "completed")

    def test_page_replaced_before_start_is_expired_not_indeterminate(self):
        c, _ = make()
        c.enqueue({"op": "write", "req": "a"})
        c.claim("OLD")
        c.claim("NEW")
        self.assertEqual((c.status("a")["state"], c.status("a")["executed"]), ("expired", False))

    def test_3_http_client_leaving_does_not_touch_a_running_command(self):
        c, clock = make(ttl=1, ack_timeout=1)
        c.enqueue({"op": "write", "req": "w"})
        w = c.claim("P"); c.ack("w", w["claim"], "P")
        clock.t += 3600                                          # nobody asked for the result for an hour
        self.assertIsNone(c.claim("P"))
        self.assertEqual(c.status("w")["state"], "running")      # not expired, not interrupted
        c.finish({"req": "w", "ok": False, "state": "indeterminate", "error": "timeout"})
        self.assertEqual(c.result("w")["state"], "indeterminate")

    def test_result_states_are_coherent(self):
        c, _ = make()
        for i, (body, want, ok) in enumerate([({"ok": True}, "completed", True), ({"ok": False}, "failed", False),
                                              ({"ok": True, "state": "indeterminate"}, "indeterminate", False),
                                              ({"ok": False, "state": "completed"}, "completed", False)]):
            c.enqueue({"op": "x", "req": str(i)})
            cm = c.claim("P"); c.ack(str(i), cm["claim"], "P")
            c.finish(dict(body, req=str(i)))
            r = c.result(str(i))
            self.assertEqual((r["state"], r["ok"]), (want, ok), body)

    def test_result_for_a_command_that_never_started_is_refused(self):
        c, _ = make()
        c.enqueue({"op": "x", "req": "a"})
        self.assertEqual(c.finish({"req": "a", "ok": True})[0], 409)

    def test_events_and_status_carry_no_payload(self):
        c, _ = make()
        c.enqueue({"op": "tx", "req": "t", "hex": "deadbeefcafe", "body": "password=hunter2"})
        cm = c.claim("P"); c.ack("t", cm["claim"], "P"); c.finish({"req": "t", "ok": True})
        blob = json.dumps(c.drain_events()) + json.dumps(c.status("t"))
        self.assertNotIn("deadbeef", blob); self.assertNotIn("hunter2", blob)
        self.assertIn('"event": "running"', blob)

    def test_finished_commands_are_bounded_but_in_flight_ones_never_evicted(self):
        c, _ = make(keep=5)
        c.enqueue({"op": "keepme", "req": "q"})
        for i in range(20):
            c.enqueue({"op": "x", "req": f"x{i}", "ttl": 0})
        c.claim("P")
        self.assertLessEqual(len(c.recs), 5 + 1)
        self.assertIn("q", c.recs)


if __name__ == "__main__":
    unittest.main()

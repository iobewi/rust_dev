"""Q3 - coordinated server restart at rest (no hardware command, no pending write).

Needs three explicit enablements: --qual-allow-restart, q3.allow_restart in the private session, and a
launcher pid file that is verified (not trusted) against /proc. A URL alone never authorises signalling
a local process. The lock file and the journal are never removed; a shutdown timeout is reported, not forced.
Hardware status: NOT executed (the identification/restart helpers are exercised against an isolated
simulated server in python/tests, which is a software check only).
"""
from urllib.parse import urlparse

import pytest

from iobewi_bench.client import ContinuityError, PendingOperation
from iobewi_bench.qualification import Campaign

from .harness import Blocked, Outcome, new_run_dir, operator_gesture, recorded, wait_until
from .restart import (LauncherError, child_gone, identify_launcher, replacement, request_restart,
                      storage_fingerprint, wait_for)
from .session import build_manifest

pytestmark = pytest.mark.bench


def test_q3_coordinated_restart_at_rest(bench_client, qual_session, pytestconfig):
    cfg, q3 = qual_session, qual_session["q3"]
    if not (pytestconfig.getoption("--qual-allow-restart") and q3["allow_restart"] and q3["launcher_pid_file"] and q3["state_dir"]):
        pytest.skip("BLOCKED: restart not enabled (--qual-allow-restart, q3.allow_restart, launcher_pid_file, state_dir)")
    port = urlparse(pytestconfig.getoption("--bench-url")).port
    manifest = build_manifest(cfg, "Q3", regions=[], steps=["Browser agent may need a page reload to reconnect"])
    case = Campaign(bench_client, cfg["board_id"], new_run_dir(cfg, "Q3"), __file__, "Q3", manifest=manifest)
    out = Outcome()
    with recorded(case, "Q3", out, preconditions=f"{cfg['chip']} / {cfg['transport']}, idle card, durable server",
                  limits="Idle restart only; no write in flight; agent reconnection may need the operator; one run"):
        case.start()
        before_health = bench_client.health()
        assert before_health["status"] == "ok" and before_health["durability"] == "sqlite-full"
        try:
            launcher = identify_launcher(q3["launcher_pid_file"], port)
        except LauncherError as error:
            raise Blocked(f"Launcher not identified: {error}") from error
        stored = storage_fingerprint(q3["state_dir"])
        old_board = bench_client.reserve(cfg["board_id"], ttl=300)     # not tracked by Campaign: it must go stale
        generation = bench_client.device(cfg["board_id"])["generation"]
        out.evidence.append(f"launcher pid {launcher.pid}, child {launcher.child}, epoch {before_health['epoch']}")

        case.interrupt_window("coordinated server restart")           # explicit end of the proof window
        request_restart(launcher)
        if not wait_for(lambda: child_gone(launcher), q3["stop_timeout_s"] + 15):
            raise PendingOperation("Old server child still running; replacement refused by the launcher. "
                                   "Inspect only: lock and journal retained, nothing forced")
        out.evidence.append("old server child exited before replacement")
        fresh = wait_for(lambda: replacement(launcher), q3["health_timeout_s"])
        assert fresh and fresh != launcher.child, "No replacement child under the same launcher"

        def healthy():
            try:
                return bench_client.health()
            except Exception:
                return None
        after = wait_for(healthy, q3["health_timeout_s"])
        assert after and after["status"] == "ok" and after["durability"] == "sqlite-full"
        assert after["epoch"] != before_health["epoch"], "Epoch must change on restart"
        later = storage_fingerprint(q3["state_dir"])
        assert later["db_inode"] == stored["db_inode"] and later["lock_inode"] == stored["lock_inode"], "Journal/lock replaced"
        assert later["db_size"] >= stored["db_size"]
        with pytest.raises(ContinuityError):
            old_board._check_target()                                 # old handles never become valid again
        out.evidence.append("epoch changed; same journal and lock files; old Board handle rejected")

        operator_gesture(case, "If the page does not reconnect by itself, reload it / reconnect the port",
                         timeout=q3["agent_reconnect_timeout_s"])
        wait_until(lambda: bench_client.device(cfg["board_id"])["connected"], timeout=q3["agent_reconnect_timeout_s"],
                   what="agent reconnection")
        state = bench_client.device(cfg["board_id"])
        assert state["privileged_seconds"] == 0 and state["reservation"] is None, "Grant/reservation survived the restart"
        assert not state["quarantined"], "An idle restart left the card quarantined"
        out.evidence.append(f"agent reconnected; generation {generation[:8]}->{state['generation'][:8]}; grant 0; no reservation")
        case.open_window("after coordinated server restart")          # fresh handles and cursor only

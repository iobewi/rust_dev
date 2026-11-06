"""Q3 - coordinated server restart at rest (no hardware command, no pending write).

Needs explicit enablement: --qual-allow-restart, q3.allow_restart in the private session, a launcher pid
file, the state_dir and the launcher log. A URL alone never authorises signalling a local process: the
endpoint must be loopback and the listening socket and the lock/journal files must belong to ONE verified
server (see restart.py). The lock file and the journal are never removed; a shutdown timeout is reported,
not forced. Hardware status: NOT executed. The flow is exercised in python/tests against isolated
simulated servers and real throw-away launchers (software only).
"""
import pytest

from .harness import Outcome, make_campaign, recorded
from .q3_flow import coordinated_restart
from .session import build_manifest

pytestmark = pytest.mark.bench


def test_q3_coordinated_restart_at_rest(bench_client, qual_session, pytestconfig):
    cfg, q3 = qual_session, qual_session["q3"]
    if not (pytestconfig.getoption("--qual-allow-restart") and q3["allow_restart"] and q3["launcher_pid_file"]
            and q3["state_dir"] and q3["launcher_log"]):
        pytest.skip("BLOCKED: restart not enabled (--qual-allow-restart, q3.allow_restart, launcher_pid_file, "
                    "state_dir, launcher_log)")
    manifest = build_manifest(cfg, "Q3", regions=[], steps=["Browser agent may need a page reload to reconnect"])
    case = make_campaign(bench_client, cfg, "Q3", __file__, pytestconfig, manifest=manifest)
    out = Outcome()
    with recorded(case, "Q3", out, preconditions=f"{cfg['chip']} / {cfg['transport']}, idle card, durable server",
                  limits="Idle restart only; no write in flight; precondition: no unreconciled/indeterminate "
                         "operation and no agent hold (otherwise reconcile, never mask); one run"):
        case.start()
        coordinated_restart(case, bench_client, cfg, pytestconfig.getoption("--bench-url"), out)

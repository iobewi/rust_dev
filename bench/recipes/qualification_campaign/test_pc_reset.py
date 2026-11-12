"""PC-RESET (G1) - ONE explicit reset and the ROM banner on the ESP32-P4 / ESP32-C6, kept apart from the reads.

Needs --qual-allow-reset, the PC-RESET consent in the private session and --bench. A reset restarts the card on purpose.
On the C6 behind the H7 CP2102 the reset capability is announced from the USB IDs only: whether DTR/RTS reach EN is unknown, so a missing
banner is reported as a FAIL of the capability on this wiring (a bench/wiring finding), never as a pass. Criteria: readonly.reset_and_observe.
Hardware status: NOT executed.
"""
import pytest

from .harness import Outcome, make_campaign, recorded
from .pc import gate
from .readonly import record_transport, reset_and_observe, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench


def test_pc3_explicit_reset_and_rom_banner(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    gate(cfg, "PC-RESET")
    if not pytestconfig.getoption("--qual-allow-reset"):
        pytest.skip("BLOCKED: --qual-allow-reset not given (an explicit reset is a deliberate restart of the card)")
    manifest = build_manifest(cfg, "PC", regions=[], steps=["The card is restarted on purpose"])
    case, out = make_campaign(bench_client, cfg, "PC-3", __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, "PC-3", out, preconditions=f"{cfg['chip']} declared {cfg['transport']}",
                  limits="One reset through the bench command; the ROM banner proves an emission after the cursor, not a correct application "
                         "boot; EN wiring of the bridge is inferred, not measured"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=300)
        reset_and_observe(case, board, bench_client, cfg, out)

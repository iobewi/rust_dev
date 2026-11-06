"""C3-R3 - ONE explicit reset and the ROM banner, kept apart from the reads.

Needs three things: --qual-allow-reset, the C3R-RESET consent in the private session, and --bench. A reset
restarts the card on purpose; on a native USB Serial/JTAG interface it may also re-enumerate the USB device.
The outcome criteria are written in readonly.reset_and_observe() before any run. Hardware status: NOT executed.
"""
import pytest

from .harness import Outcome, make_campaign, recorded
from .readonly import record_transport, require_consent, require_target, reset_and_observe, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench


def test_c3r3_explicit_reset_and_rom_banner(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    require_target(cfg)
    if not pytestconfig.getoption("--qual-allow-reset"):
        pytest.skip("BLOCKED: --qual-allow-reset not given (an explicit reset is a deliberate restart of the card)")
    require_consent(cfg, "C3R-RESET")
    manifest = build_manifest(cfg, "C3R", regions=[], steps=["The card is restarted on purpose"])
    case, out = make_campaign(bench_client, cfg, "C3R-3", __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, "C3R-3", out, preconditions=f"{cfg['chip']} declared {cfg['transport']}",
                  limits="One reset; the ROM banner proves an emission after the cursor, not a correct application boot; "
                         "native-USB re-enumeration is reported INDETERMINATE until a criterion is written from results"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=300)
        reset_and_observe(case, board, bench_client, cfg, out)

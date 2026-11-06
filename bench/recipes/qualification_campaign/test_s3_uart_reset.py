"""S3R-1 (issue #2, requirement 1) - ONE explicit EN reset of an ESP32-S3 behind an EXTERNAL UART bridge, and the ROM banner.

Criteria, written before any run (the code is the contract; nothing is adjusted during a run):
  PASS            `reset` settles and the marker (default ``ESP-ROM:esp32s3``) is seen after the cursor taken BEFORE the reset,
                  in the same proof window, with no continuity break. On an external UART bridge the USB port of the host is
                  not the card's: a continuity break is NOT expected here;
  INDETERMINATE   continuity broke, or the reset outcome is unknown (PendingOperation): window closed explicitly, never replayed;
  FAIL            the reset settled, nothing broke and no banner within the bound;
  BLOCKED         session not for an esp32s3 / external_uart, consent S3R-RESET or --qual-allow-reset missing, no reset capability.
The ROM banner proves an emission after the cursor, NOT a correct application boot (``boot_verified`` is always false).
BOOT hold (IO0) and the console return after a loader operation are separate recipes, see docs/issue-2-phase2-contracts.md.
Needs --bench, --qual-allow-reset and the S3R-RESET consent in the private session. Hardware status: NOT executed.
"""
import pytest

from .harness import Outcome, make_campaign, recorded
from .readonly import record_transport, require_consent, require_target, reset_and_observe, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench


def test_s3r1_en_reset_and_rom_banner(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    require_target(cfg, "esp32s3", "external_uart")
    if not pytestconfig.getoption("--qual-allow-reset"):
        pytest.skip("BLOCKED: --qual-allow-reset not given (an EN reset is a deliberate restart of the card)")
    require_consent(cfg, "S3R-RESET")
    manifest = build_manifest(cfg, "S3R", regions=[], steps=["The card is restarted on purpose (EN reset through the UART bridge)"])
    case, out = make_campaign(bench_client, cfg, "S3R-1", __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, "S3R-1", out, preconditions=f"{cfg['chip']} declared {cfg['transport']}",
                  limits="One EN reset; the ROM banner proves an emission after the cursor, not a correct application boot; "
                         "BOOT hold and console return after a loader operation are not covered by this recipe"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=300)
        reset_and_observe(case, board, bench_client, cfg, out)

"""S3R-2 (issue #2, requirement 1) - ROM download mode through ONE atomic `boot.download` command (IO0 low + EN reset, bounded hold,
both lines released), S3 behind an EXTERNAL UART bridge.

Criteria, written before any run:
  PASS            `boot.download` settles and the session's download-mode marker is seen after the cursor taken BEFORE the command,
                  in the same proof window, no continuity break. The marker comes from the ROM documentation and the session
                  (``bootloader.marker``); it has no default and is never adjusted from a result;
  INDETERMINATE   continuity broke or the outcome is unknown (PendingOperation): window closed explicitly, never replayed;
  FAIL            the command settled, nothing broke and no marker within the bound;
  BLOCKED         not an esp32s3/external_uart session, S3R-BOOT consent or --qual-allow-bootloader missing, no marker in the session,
                  no ``boot.download`` capability, or the local grant is not observed.
Needs a LOCAL operator grant: the operator is asked for the SHORTEST grant and then does nothing (natural expiry, no revocation).
The card is left in ROM download mode until its next reset: ending that is an operator step, not part of this recipe.
Entering download mode proves nothing about application boot (``boot_verified`` is always false). Hardware status: NOT executed.
"""
import hashlib

import pytest

from iobewi_bench.client import ContinuityError, PendingOperation

from .harness import Blocked, Outcome, make_campaign, operator_gesture, recorded, wait_until
from .readonly import record_transport, require_consent, require_target, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench
ASK_SHORT, MIN_SHORT = 60, 10


def test_s3r2_boot_download_marker(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    require_target(cfg, "esp32s3", "external_uart")
    if not pytestconfig.getoption("--qual-allow-bootloader"):
        pytest.skip("BLOCKED: --qual-allow-bootloader not given (entering the ROM download mode is a deliberate restart)")
    require_consent(cfg, "S3R-BOOT")
    boot = cfg["bootloader"]
    if "marker" not in boot:
        pytest.skip("BLOCKED: the session has no bootloader.marker (it must be fixed from the ROM documentation before the run)")
    manifest = build_manifest(cfg, "S3R", regions=[], steps=["Shortest local grant, then nothing until it expires",
                                                              "The card is left in ROM download mode"])
    case, out = make_campaign(bench_client, cfg, "S3R-2", __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, "S3R-2", out, preconditions=f"{cfg['chip']} declared {cfg['transport']}",
                  limits="One boot.download; the marker proves an emission in download mode after the cursor, not application boot; "
                         "the card stays in download mode until its next reset"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        if "boot.download" not in bench_client.device(cfg["board_id"]).get("capabilities", []):
            raise Blocked("the card exposes no boot.download capability on this interface")
        board = case.reserve(ttl=300)
        operator_gesture(case, f"GRANT the SHORTEST local duration ({ASK_SHORT} s) and then do nothing", timeout=cfg["operator_timeout_s"])
        try:
            case.client.wait_device(cfg["board_id"], timeout=cfg["operator_timeout_s"], grant_seconds=MIN_SHORT, recovered=True)
        except TimeoutError as error:
            raise Blocked("Local grant not observed") from error
        cursor = board.cursor()
        generation = bench_client.device(cfg["board_id"])["generation"]
        case.client.record("bootloader-requests.jsonl", {"hold_ms": boot["hold_ms"], "marker_len": len(boot["marker"]),
                                                       "marker_sha256": hashlib.sha256(boot["marker"].encode()).hexdigest(), "generation": generation[:8]})
        try:
            result = board.command("boot.download", {"hold_ms": boot["hold_ms"]}, timeout=60)
            out.evidence.append("boot.download settled; result fields: " + ", ".join(sorted(result or {})))
            board.wait_uart(boot["marker"].encode(), cursor=cursor, timeout=boot["marker_timeout_s"])
        except (ContinuityError, PendingOperation) as error:
            out.evidence.append(f"continuity broke or outcome unknown ({type(error).__name__}): not replayed, not certified")
            case.interrupt_window("boot.download: continuity break or indeterminate outcome")
            raise
        except TimeoutError as error:
            raise AssertionError(f"boot.download settled without continuity break but the marker was not seen within "
                                 f"{boot['marker_timeout_s']} s") from error
        out.evidence.append("download-mode marker observed after the command, same proof window")
        wait_until(lambda: bench_client.device(cfg["board_id"])["privileged_seconds"] == 0, timeout=ASK_SHORT + 60, interval=1.0,
                   what="the local grant expires on its own")
        out.evidence.append("the grant expired naturally; the card is left in ROM download mode")

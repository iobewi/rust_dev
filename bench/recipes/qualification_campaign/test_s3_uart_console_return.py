"""S3R-3 (issue #2, requirement 1; requirement 6 in part) - console return after a LOADER operation, S3 behind an EXTERNAL UART bridge.

Criteria, written before any run:
  PASS            the `flash.read` of one range already used read-only is SETTLED (the broker accepted its result: Board.command only
                  returns a settled operation) AND a transport event "console opened" is recorded after the cursor taken before the
                  command, with no continuity break. This is a proof about the TRANSPORT (the console was reopened). A text on the UART is
                  not accepted as the proof, and nothing here says that the application restarted correctly (boot_verified is always false);
  INDETERMINATE   continuity broke or the outcome is unknown (PendingOperation); never replayed;
  FAIL            the read settled and no console-return event followed;
  BLOCKED         session not for an esp32s3/external_uart, S3R-READ consent missing, no readonly.reads range.
No write, no erase, no reset, no grant. A distinct deterministic console marker is NOT required here; asking for one is a different recipe
because the application is not part of the bench. Hardware status: NOT executed.
"""
import pytest

from .harness import Outcome, make_campaign, recorded
from .readonly import (_console_after_early_exit, record_transport, require_consent, require_target, retained_reads, sha256,
                       start_or_block)
from .session import build_manifest

pytestmark = pytest.mark.bench


def test_s3r3_console_returns_after_a_loader_read(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    require_target(cfg, "esp32s3", "external_uart")
    require_consent(cfg, "S3R-READ")
    if not cfg["readonly"]["reads"]:
        pytest.skip("BLOCKED: no readonly.reads range configured (use a range already qualified as stable)")
    item = cfg["readonly"]["reads"][0]
    manifest = build_manifest(cfg, "S3R", regions=[], steps=["No operator gesture; the loader entry may restart the card"])
    case, out = make_campaign(bench_client, cfg, "S3R-3", __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, "S3R-3", out, preconditions=f"{cfg['chip']} declared {cfg['transport']}",
                  limits="TRANSPORT proof only: the console was reopened after a loader read; no application marker, no boot verification; "
                         "one read, one run"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=300)
        try:
            with retained_reads(board, out) as reads:
                cursor = board.cursor()
                cursor_start = cursor.after
                data = reads.read(address=item["address"], size=item["size"], return_console=True)
                events = board.events(cursor)
        except BaseException as error:
            _console_after_early_exit(board, out, error)
            raise
        details = [event.get("detail", "") for event in events if event["kind"] == "transport"]
        # Raw transport events (agent transport messages, no UART content) with the cursor they follow: the comparator correlates them
        # with the read operation instead of trusting the sentence below.
        for event in events:
            if event["kind"] == "transport":
                case.client.record("console-events.jsonl", {"seq": event.get("seq"), "kind": event["kind"], "detail": str(event.get("detail", ""))[:120],
                                                           "cursor_index": cursor.index, "cursor_after_start": cursor_start,
                                                           "operation": (board.last_operation or {}).get("id")})
        out.evidence.append(f"read {item['address']:#x}+{item['size']:#x} settled (sha256={sha256(data)}); transport events after the cursor: "
                            + "; ".join(details))
        assert any("console opened" in detail for detail in details), (
            "the loader read settled but no 'console opened' transport event followed: console return is not shown")
        out.evidence.append("console return shown by a transport event after the settled read (TRANSPORT proof only; not an application boot)")

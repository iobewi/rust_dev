"""U8 - sustained UART capture seen by an EXTERNAL archiving client (issue #2, requirement 8). Read-only: no reservation, no command, no grant.

The bench does not provide the SOURCE: the card must already run a firmware printing continuously (for example the project's own console
log). The recipe archives the event stream with tools/sse_capture.py, drops and resumes its connection at the last cursor every
`resume_every_s`, and recomputes continuity from the index it wrote (metadata only; the archive is private evidence, never in Git).

Criteria, written before any run (the numeric thresholds are in the pinned criteria, the window in the private session):
  PASS             over the whole window: bytes >= capture.min_bytes (a source exists), NO silent loss (every gap in source_seq is announced by
                   source_lost), no duplicate event, no server-side loss (`lost` = 0), flagged losses <= capture.max_flagged_loss, at least one
                   deliberate resume, one epoch and no disconnection;
  FAIL             a silent loss, a duplicate, a server-side loss, or too many flagged losses (losses are REPORTED with their count, never hidden);
  INDETERMINATE    the epoch or the generation changed, or the card disconnected during the window (continuity broken);
  BLOCKED          no consent / no capture section / fewer bytes than capture.min_bytes (no sustained source: nothing is concluded).
Background tab: the operator is asked to keep the page in a background tab during the window; the bench has no signal of the tab state, so this
is an announced gesture, not a verified condition. Hardware status: NOT executed.
"""
import json
import sys
from pathlib import Path

import pytest

from .harness import Blocked, Outcome, make_campaign, operator_gesture, recorded
from .readonly import record_transport, require_consent, start_or_block
from .session import build_manifest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import sse_capture                                                            # noqa: E402

pytestmark = pytest.mark.bench


def test_u8_sustained_capture_with_loss_detection(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    require_consent(cfg, "U8")
    window = cfg["capture"]
    if not window:
        pytest.skip("BLOCKED: no capture section in the session (duration_s, min_bytes)")
    manifest = build_manifest(cfg, "U8", regions=[], steps=["Keep the page in a BACKGROUND tab during the capture window"])
    case, out = make_campaign(bench_client, cfg, "U8", __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, "U8", out, preconditions=f"{cfg['chip']} declared {cfg['transport']}, window {window['duration_s']} s, >= {window['min_bytes']} bytes",
                  limits="Observation of an EXISTING source by one external client; the tab state is not verified; the source rate is the firmware's, "
                         "not the bench's; no endurance claim; one run"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        operator_gesture(case, "Keep the bench page in a BACKGROUND tab during the capture window, bring it back at the end", timeout=window["duration_s"])
        # A sustained source overflows the server's bounded event ring between the opening and the closing of the campaign's own proof window
        # (Campaign.close() refuses a window with lost events). The external archiving client below is the witness of THIS scenario, so the
        # window is closed explicitly instead of being allowed to fail at the end.
        case.interrupt_window("U8: a sustained UART stream outlasts the server ring by design; the external client is the witness")
        stats = sse_capture.capture(bench_client, cfg["board_id"], window["duration_s"], index_path=case.directory / "capture-index.jsonl",
                                    archive_path=case.directory / "uart-archive.bin", resume_every_s=window["resume_every_s"])
        stats["thresholds"] = {"min_bytes": window["min_bytes"], "max_flagged_loss": window["max_flagged_loss"]}
        case.write("capture-stats.json", stats)
        out.evidence.append(f"capture: {stats['bytes']} bytes in {stats['events']} events over {stats['seconds']} s, {stats['resumes']} deliberate resume(s), "
                            f"silent_loss={stats['silent_loss']} flagged_loss={stats['flagged_loss']} duplicates={stats['duplicates']} server_lost={stats['server_lost']}")
        if stats["bytes"] < window["min_bytes"]:
            raise Blocked(f"only {stats['bytes']} bytes were received (< {window['min_bytes']}): the card runs no sustained UART source")
        if len(stats["epochs"]) != 1 or stats["disconnected_events"] or stats["generations"] > 1:
            raise AssertionError("continuity broke during the window (epoch, generation or disconnection): not a plain capture")
        assert stats["silent_loss"] == 0, f"{stats['silent_loss']} silently lost chunk(s)"
        assert stats["duplicates"] == 0 and stats["server_lost"] == 0, f"duplicates={stats['duplicates']} server_lost={stats['server_lost']}"
        assert stats["flagged_loss"] <= window["max_flagged_loss"], f"{stats['flagged_loss']} flagged losses (> {window['max_flagged_loss']})"
        assert stats["resumes"] >= 1, "the connection was never dropped and resumed"
        out.evidence.append("continuity proven: no silent loss, no duplicate, losses (if any) announced and within the bound; resumed at the cursor without gap")

"""M2 - two cards of the SAME chip family connected together (issue #2, requirement 9). Needs a second REAL card: never simulated in a campaign.

The operator opens a second INDEPENDENT browser session (another browser, another profile, or one normal and one private window) and connects
the second card under another logical id (`second_board.board_id` in the private session, allowed by the server's token configuration), setting
that id BEFORE connecting the port. Tabs or windows of the same profile share the session cookie, hence the same agent: they interfere with each
other and both default to `esp32-local` (observed during the first M2 attempt). Loader entry (identify, flash.read) may restart each card (M2 consent). No write.

Criteria, written before any run:
  PASS     both cards are connected, unreserved, unquarantined and of the declared chip; one client reserves BOTH at the same time; each
           identify measures the declared chip; the same immutable range is read from both; releasing the first reservation leaves the
           second one held and working, and the first card can be reserved again; no operation is unresolved;
  FAIL     a measured chip differs, a reservation of one card blocks or releases the other, or a command lands on the wrong card;
  BLOCKED  no consent / no second_board in the session / the second card is not connected / the chips differ.
Equal flash ids of the two cards are only reported (two units of one model may share them). Hardware status: NOT executed.
"""
import pytest

from iobewi_bench.client import BenchError

from .harness import Blocked, Outcome, make_campaign, recorded
from .readonly import record_transport, require_consent, retained_reads, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench


def test_m2_two_boards_of_the_same_family(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    require_consent(cfg, "M2")
    other_id = cfg["second_board"].get("board_id")
    if not other_id:
        pytest.skip("BLOCKED: no second_board.board_id in the session (a second real card is required)")
    if not cfg["readonly"]["reads"]:
        pytest.skip("BLOCKED: no readonly.reads range configured (use a range already qualified as stable)")
    item = cfg["readonly"]["reads"][0]
    manifest = build_manifest(cfg, "M2", regions=[], steps=["Second card connected in an independent second browser session under its own logical id"])
    case, out = make_campaign(bench_client, cfg, "M2", __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, "M2", out, preconditions=f"{cfg['chip']} x2 ({cfg['board_id']}, {other_id})",
                  limits="Two real cards of one family reserved together; reads of one immutable range; no grant, no write; loader entry may restart "
                         "each card; says nothing about more than two cards or about simultaneous heavy transfers; one run"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        first = bench_client.device(cfg["board_id"])
        try:
            second = bench_client.device(other_id)
        except BenchError as error:
            raise Blocked(f"the second card {other_id!r} is unknown to the server (not connected under that logical id)") from error
        problems = [m for ok, m in ((second["connected"], f"{other_id} is not connected"), (second["chip"] == cfg["chip"], f"{other_id} reports chip {second['chip']}"),
                                    (not second["quarantined"], f"{other_id} is quarantined"), (second["reservation"] is None, f"{other_id} is reserved"),
                                    (first["generation"] != second["generation"], "both cards report the same generation")) if not ok]
        if problems:
            raise Blocked("; ".join(problems))
        a = case.reserve(ttl=600)
        b = case.client.reserve(other_id, ttl=600, output_dir=case.directory / "board-second")
        try:
            out.evidence.append("one client holds a reservation on each of the two cards at the same time")
            ids = {}
            for label, board in (("first", a), ("second", b)):
                identity = board.identify()
                ids[label] = {k: identity.get(k) for k in ("chip", "flash_size", "flash_id")}       # never the MAC
                assert identity["chip"] == cfg["chip"], f"{label} card measured {identity['chip']}, declared {cfg['chip']}"
            case.write("identities.json", ids)
            out.evidence.append(f"both cards measured as {cfg['chip']}; flash ids equal: {ids['first']['flash_id'] == ids['second']['flash_id']} (reported only)")
            digests = []
            for board in (a, b):
                with retained_reads(board, out) as reads:
                    digests.append(reads.read(address=item["address"], size=item["size"]))
            out.evidence.append(f"the same range {item['address']:#x}+{item['size']:#x} was read from both cards; equal: {digests[0] == digests[1]} (reported only)")
            a.close()
            assert bench_client.device(cfg["board_id"])["reservation"] is None, "closing the first reservation did not free the first card"
            assert bench_client.device(other_id)["reservation"] is not None, "closing the first reservation released the second card"
            b.identify()
            out.evidence.append("the first reservation was released; the second card stayed reserved and answered")
            again = case.reserve(ttl=120)
            again.identify()
            out.evidence.append("the first card was reserved again and answered")
        finally:
            try:
                b.close()
            except Exception:           # noqa: BLE001 - reported by Campaign/recorded through the verdict if it matters
                pass

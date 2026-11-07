"""NUSB - a chip reached through its NATIVE USB Serial/JTAG interface (issue #2, requirement 2). Never an external UART bridge and never USB OTG.

The three transports are not interchangeable: only a card whose declared AND agent-inferred transport is `usb_serial_jtag` is accepted.
An OTG device (`native_usb_other`), a bridge (`external_uart`) or an unknown inference is BLOCKED, never a PASS and never a FAIL.
Loader entry may restart the card (NUSB-READ consent); the explicit reset has its own opt-in and consent (NUSB-RESET). No flash write, no erase.

NUSB-0  Criteria, written before any run:
  PASS            measured chip and flash size equal the declaration; the capabilities contain `reset` and neither `io0` nor
                  `boot.download`; both commands are refused `unsupported` (HTTP 400) before any hardware effect; no operation left;
  FAIL            a measured value differs, a forbidden capability is offered, or a refusal is another code / is accepted;
  BLOCKED         not the declared native transport, no consent, card absent.
NUSB-1  two reads of every configured immutable range are equal; exactly one valid partition table, the raw decoder equals the API view.
NUSB-2  ONE reset (see reset_and_characterize): the ROM banner of the chip is seen after the cursor, EITHER in the same proof window
        (no re-enumeration) OR after a re-enumeration that is observed, closed explicitly, followed by a reconnection with a NEW
        generation inside the bound and a freshly opened window. Both are PASS; the recorded `reenumeration.json` says which. An unknown
        outcome (PendingOperation) or a missing reconnection is INDETERMINATE: never replayed, never a PASS.
The banner / the new generation prove a restart and a USB return, never a correct application boot (`boot_verified` is false).
Hardware status: NOT executed.
"""
import pytest

from iobewi_bench.client import ContinuityError, PendingOperation
from iobewi_bench.qualification import expect_http_refusal

from .harness import Blocked, Outcome, abandon_stale, make_campaign, recorded, seal, wait_until
from .readonly import (check_rows, console_on_exit, decode_table, identify_and_confirm_console, probe_candidates, record_transport,
                       require_consent, require_target, retained_reads, sha256, start_or_block)
from .session import build_manifest, table_candidates

pytestmark = pytest.mark.bench
LIMITS = ("Native USB Serial/JTAG only; loader entry may restart the card; transport is a declaration plus an agent inference, not a "
          "measurement; USB OTG is NOT covered; one run; says nothing about firmware validity or boot policy")


def _gate(cfg, scenario):
    if cfg["chip"] not in {"esp32s3", "esp32c3"}:
        pytest.skip("BLOCKED: unsupported chip")
    require_target(cfg, cfg["chip"], "usb_serial_jtag")
    require_consent(cfg, scenario)


def _case(bench_client, cfg, test_id, pytestconfig, step):
    manifest = build_manifest(cfg, "NUSB", regions=[], steps=[step])
    return make_campaign(bench_client, cfg, test_id, __file__, pytestconfig, manifest=manifest)


def _require_native_inference(bench_client, cfg):
    provenance = bench_client.device(cfg["board_id"]).get("provenance") or {}
    inferred = provenance.get("transport", "unknown")
    if inferred != "usb_serial_jtag":
        raise Blocked(f"the agent infers transport {inferred!r}, not usb_serial_jtag: this card is not reached through the native "
                      "USB Serial/JTAG interface (USB OTG and UART bridges are separate qualifications)")


def test_nusb0_identity_and_refusals(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    _gate(cfg, "NUSB-READ")
    case, out = _case(bench_client, cfg, "NUSB-0", pytestconfig, "No operator gesture; the card may restart on loader entry"), Outcome()
    with recorded(case, "NUSB-0", out, limits=LIMITS, preconditions=f"{cfg['chip']} declared {cfg['transport']}"):
        start_or_block(case)
        _require_native_inference(bench_client, cfg)
        record_transport(case, bench_client, cfg, out)
        capabilities = set(bench_client.device(cfg["board_id"]).get("capabilities", []))
        assert "reset" in capabilities, "a Serial/JTAG card must offer reset"
        assert not capabilities & {"io0", "boot.download"}, f"EN/BOOT commands offered on a native interface: {sorted(capabilities & {'io0', 'boot.download'})}"
        out.evidence.append("capabilities: reset offered; io0 and boot.download absent")
        board = case.reserve(ttl=300)
        identity = identify_and_confirm_console(board, out)
        case.write("identity.json", {k: identity.get(k) for k in ("chip", "flash_size", "flash_id")})   # never the MAC
        assert identity["chip"] == cfg["chip"] and identity["flash_size"] == cfg["flash_size"], f"measured {identity} differs from the declaration"
        out.evidence.append(f"measured chip={identity['chip']} flash_size={identity['flash_size']} (equal to the declaration)")
        for command, args in (("io0", {"duration_ms": 100}), ("boot.download", {"hold_ms": 500})):
            expect_http_refusal(lambda: board.command(command, args), status=400, code="unsupported")
            out.evidence.append(f"{command} refused 400 unsupported before any hardware effect")
        assert not board.pending and not board.uncertain


def test_nusb1_reads_and_table(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    _gate(cfg, "NUSB-READ")
    candidates = table_candidates(cfg)
    if not cfg["readonly"]["reads"] or not candidates:
        pytest.skip("BLOCKED: configure readonly.reads and a partition table candidate (none is presumed)")
    case, out = _case(bench_client, cfg, "NUSB-1", pytestconfig, "No operator gesture; the card may restart on loader entry"), Outcome()
    with recorded(case, "NUSB-1", out, limits=LIMITS, preconditions=f"{cfg['chip']} declared {cfg['transport']}"):
        start_or_block(case)
        _require_native_inference(bench_client, cfg)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=300)
        with console_on_exit(board, out), retained_reads(board, out) as reads:
            for item in cfg["readonly"]["reads"]:
                first, second = reads.read(address=item["address"], size=item["size"]), reads.read(address=item["address"], size=item["size"])
                assert len(first) == len(second) == item["size"] and first == second, f"two reads of {item['address']:#x} differ"
                out.evidence.append(f"{item['address']:#x}+{item['size']:#x}: two reads equal, sha256={sha256(first)}")
            valid, probes = probe_candidates(board, candidates)
            case.write("table-probes.json", probes)
            if len(valid) != 1:
                raise Blocked(f"{len(valid)} valid table(s) among {[hex(a) for a in candidates]}")
            address, rows = valid[0]
            assert decode_table(reads.read(address=address, size=4096)) == [(r["label"], r["address"], r["size"]) for r in rows], "raw table differs from the API view"
            check_rows(rows, cfg["flash_size"])
            out.evidence.append(f"one valid table at {address:#x}: " + ", ".join(f"{r['label']}@{r['address']:#x}+{r['size']:#x}" for r in rows))


def reset_and_characterize(case, board, client, cfg, out):
    """ONE reset. Returns "same_window" or "reenumerated"; raises (never PASS) for every unknown or incomplete outcome."""
    reset, board_id = cfg["readonly"]["reset"], cfg["board_id"]
    device = client.device(board_id)
    if "reset" not in device.get("capabilities", []):
        raise Blocked("the card exposes no reset capability on this interface")
    generation, cursor = device["generation"], board.cursor()
    case.client.record("reset-requests.jsonl", {"marker": reset["marker"], "generation": generation[:8]})
    try:
        result = board.command("reset", {}, timeout=60)
        out.evidence.append("reset settled; result fields: " + ", ".join(sorted(result or {})))
        board.wait_uart(reset["marker"].encode(), cursor=cursor, timeout=reset["marker_timeout_s"])
    except PendingOperation:
        out.evidence.append("the reset outcome is UNKNOWN (indeterminate operation): not replayed, not certified")
        case.interrupt_window("reset: indeterminate outcome")
        raise
    except ContinuityError:
        case.interrupt_window("reset: USB re-enumeration or disconnect")
        wait_until(lambda: client.device(board_id)["connected"] and client.device(board_id)["generation"] != generation,
                   timeout=reset["reconnect_timeout_s"], interval=0.5, what="card reconnected with a new generation")
        after = client.device(board_id)["generation"]
        case.open_window("after reset re-enumeration")
        case.write("reenumeration.json", {"observed": True, "generation_before": generation[:8], "generation_after": after[:8]})
        out.evidence.append(f"re-enumeration observed: window closed explicitly, card reconnected with a new generation ({generation[:8]} -> {after[:8]}), new window opened")
        return "reenumerated"
    except TimeoutError as error:
        raise AssertionError(f"reset settled without continuity break but no '{reset['marker']}' within {reset['marker_timeout_s']} s") from error
    case.write("reenumeration.json", {"observed": False, "generation_before": generation[:8], "generation_after": generation[:8]})
    out.evidence.append(f"'{reset['marker']}' observed after the reset, same proof window")
    return "same_window"


def test_nusb2_reset_and_reenumeration(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    _gate(cfg, "NUSB-RESET")
    if not pytestconfig.getoption("--qual-allow-reset"):
        pytest.skip("BLOCKED: --qual-allow-reset not given (an explicit reset is a deliberate restart of the card)")
    case, out = _case(bench_client, cfg, "NUSB-2", pytestconfig, "The card is restarted on purpose and may re-enumerate on USB"), Outcome()
    with recorded(case, "NUSB-2", out, limits=LIMITS + "; the banner or the reconnection proves a restart, not an application boot",
                  preconditions=f"{cfg['chip']} declared {cfg['transport']}"):
        start_or_block(case)
        _require_native_inference(bench_client, cfg)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=300)
        outcome = reset_and_characterize(case, board, bench_client, cfg, out)
        if outcome == "reenumerated":
            abandon_stale(case, board, out, "USB re-enumeration after the explicit reset")   # stale by design: never resumed, never certified
            seal(board)

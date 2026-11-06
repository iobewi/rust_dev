"""C3-R support. Reads and the explicit reset are different things and live in different tests.

Reads (identify, flash.read, partitions) enter the ROM loader; that may restart the card as a side effect and
needs the C3R-READ consent. The explicit reset command is a deliberate restart: it has its own opt-in and its
own consent (C3R-RESET). Nothing here writes to flash.
"""
import hashlib
import struct

import pytest

from iobewi_bench.client import ContinuityError, OperationError

from .harness import Blocked, wait_until


def require_consent(cfg, scenario):
    if scenario not in cfg["consent"].get("scenarios", []):
        pytest.skip(f"BLOCKED: the session lacks the {scenario} consent (operator acknowledgement of loader entry / restart effects)")


def start_or_block(case):
    """A declaration that does not match the observed card is a missing precondition, not a bench failure."""
    try:
        case.start()
    except ValueError as error:
        raise Blocked(f"declared board / chip / flash / transport does not match the observed card: {error}") from error
    except AssertionError as error:
        raise Blocked(f"card not ready: {error}") from error


def record_transport(case, client, cfg, out):
    """Keep BOTH the operator's declaration and the agent's inference; neither measures the physical wiring."""
    device = client.device(cfg["board_id"])
    provenance = device.get("provenance") or {}
    record = {"declared_by_operator": cfg["transport"], "inferred_by_agent": provenance.get("transport", "unknown"),
              "inference_basis": provenance.get("transport_basis"),
              "usb_vid_pid": [provenance.get("usb_vid"), provenance.get("usb_pid")],
              "provenance_source": device.get("provenance_source", "none"),
              "unknown_accepted_by_session": cfg["accept_unknown_transport"],
              "note": "Neither value proves the physical interface; USB IDs are not a wiring measurement."}
    case.write("transport.json", record)
    out.evidence.append(f"transport declared={record['declared_by_operator']} inferred={record['inferred_by_agent']} "
                        f"({record['inference_basis'] or 'no basis'}; {record['provenance_source']})")


def decode_table(raw):
    """Independent decoder of the raw ESP partition table (entries of 32 bytes, magic 0xAA50)."""
    rows = []
    for offset in range(0, 0xC00, 32):
        entry = raw[offset:offset + 32]
        if entry[:2] != b"\xaa\x50":
            break
        _, _, start, size = struct.unpack("<BBII", entry[2:12])
        rows.append((entry[12:28].split(b"\0", 1)[0].decode("utf-8"), start, size))
    return rows


def probe_candidates(board, candidates):
    """Only partition_table_invalid lets the probe move to the next candidate; any other error propagates."""
    valid, probes = [], []
    for address in candidates:
        try:
            result = board.command("partitions", {"address": address, "return_console": False})
        except OperationError as error:
            code = (error.operation.get("error") or {}).get("code")
            if code != "partition_table_invalid":
                raise
            probes.append({"address": address, "result": code})
        else:
            rows = result["partitions"]
            probes.append({"address": address, "result": "valid", "entries": len(rows)})
            valid.append((address, rows))
    return valid, probes


def check_rows(rows, flash_size):
    previous_end = 0
    for row in sorted(rows, key=lambda r: r["address"]):
        assert row["size"] > 0 and row["address"] >= previous_end, "Partition overlap"
        previous_end = row["address"] + row["size"]
        assert previous_end <= flash_size, "Partition beyond the flash capacity"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def reset_and_observe(case, board, client, cfg, out):
    """Send ONE explicit reset and observe the ROM banner inside the same proof window.

    Criteria (decided before the run):
      PASS            the banner is seen after the cursor taken before the reset, with no continuity break;
      INDETERMINATE   continuity broke (USB re-enumeration or disconnect): the window is closed explicitly, the
                      reconnection is observed and recorded, a new window is opened, but this recipe does not call
                      that a pass - a criterion for native USB is written only after seeing real results;
      FAIL            the reset settled, nothing broke and no banner appeared within the bound;
      BLOCKED         the card does not expose a reset capability on this interface.
    """
    reset, board_id = cfg["readonly"]["reset"], cfg["board_id"]
    device = client.device(board_id)
    if "reset" not in device.get("capabilities", []):
        raise Blocked("the card exposes no reset capability on this interface")
    generation = device["generation"]
    cursor = board.cursor()
    case.client.record("reset-requests.jsonl", {"marker": reset["marker"], "generation": generation[:8]})
    try:
        result = board.command("reset", {}, timeout=60)
        out.evidence.append("reset settled; result fields: " + ", ".join(sorted(result or {})))
        board.wait_uart(reset["marker"].encode(), cursor=cursor, timeout=reset["marker_timeout_s"])
    except ContinuityError as error:
        out.evidence.append(f"continuity broke after the reset command ({type(error).__name__}): USB re-enumeration or disconnect")
        case.interrupt_window("reset: continuity break")
        try:
            wait_until(lambda: client.device(board_id)["connected"] and client.device(board_id)["generation"] != generation,
                       timeout=reset["reconnect_timeout_s"], interval=0.5, what="card reconnected with a new generation")
        except Blocked:
            out.evidence.append(f"card NOT seen reconnected with a new generation within {reset['reconnect_timeout_s']} s")
        else:
            out.evidence.append(f"card reconnected: generation {generation[:8]} -> {client.device(board_id)['generation'][:8]}")
            case.open_window("after reset re-enumeration")
        raise
    except TimeoutError as error:
        raise AssertionError(f"reset settled without continuity break but no '{reset['marker']}' within "
                             f"{reset['marker_timeout_s']} s") from error
    out.evidence.append(f"'{reset['marker']}' observed after the reset, same proof window")

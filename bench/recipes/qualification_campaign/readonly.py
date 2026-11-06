"""C3-R support. Reads and the explicit reset are different things and live in different tests.

Reads (identify, flash.read, partitions) enter the ROM loader; that may restart the card as a side effect and
needs the C3R-READ consent. The explicit reset command is a deliberate restart: it has its own opt-in and its
own consent (C3R-RESET). Nothing here writes to flash.
"""
import hashlib
import json
import struct
from contextlib import contextmanager

import pytest

from iobewi_bench.client import ContinuityError, OperationError, PendingOperation

from .harness import Blocked, describe, wait_until


TARGET_CHIP = "esp32c3"


def require_target(cfg):
    """These recipes qualify a C3. A coherent S3 session must not be able to PASS them."""
    if cfg["chip"] != TARGET_CHIP:
        pytest.skip(f"BLOCKED: the C3-R recipes target an {TARGET_CHIP}; the session declares {cfg['chip']}")


def gate(cfg, scenario):
    """Everything that can be decided from the private session alone, before any evidence or command."""
    require_target(cfg)
    require_consent(cfg, scenario)


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


class ReadbackRetention:
    """Reads with return_console=False and releases the SERVER copy of every readback, never before the private one is safe.

    Board.read_flash already stores a private local copy (flash-NNN-<addr>.bin + sidecar). A server artifact is deleted only
    after that copy was RE-READ from disk and matched the artifact's size and SHA-256. A copy that is missing or different
    keeps the server artifact (it may be the only evidence) and is reported. Cleanup never changes a FAIL / BLOCKED /
    INDETERMINATE into anything else; on a path that would otherwise PASS, a cleanup problem makes the test fail visibly."""

    def __init__(self, board, out):
        self.board, self.out, self.held = board, out, []

    def read(self, *, address, size, timeout=600):
        """Track the artifact even when read_flash fails AFTER the operation settled (download, size or disk error):
        the server copy then exists without a verified local copy, so release() keeps it and says so."""
        before = (self.board.last_operation or {}).get("id")
        try:
            return self.board.read_flash(address=address, size=size, timeout=timeout, return_console=False)
        finally:
            operation = self.board.last_operation or {}
            artifact = (operation.get("result") or {}).get("artifact")
            if operation.get("id") != before and artifact:
                self.held.append(dict(artifact))

    def local_copy_matches(self, artifact):
        directory = self.board.output_dir
        if directory is None:
            return False
        for sidecar in sorted(directory.glob("flash-*.json")):
            meta = json.loads(sidecar.read_text())
            if meta.get("artifact") != artifact["id"]:
                continue
            blob = sidecar.with_suffix(".bin")
            if not blob.is_file():
                return False
            raw = blob.read_bytes()
            digest = sha256(raw)
            expected = artifact.get("sha256", digest)
            return len(raw) == artifact.get("size", len(raw)) and digest == expected == meta.get("sha256")
        return False

    def release(self):
        if self.held and (self.board.uncertain or self.board.pending):
            self.held.clear()
            return ["an operation is unresolved: every server readback is KEPT (it may be the only evidence)"]
        problems = []
        for artifact in self.held:
            name = artifact["id"][:8]
            if not self.local_copy_matches(artifact):
                problems.append(f"readback {name}: private local copy missing or different - server artifact KEPT")
                continue
            try:
                self.board.client.delete_artifact(artifact["id"])
            except Exception as error:               # noqa: BLE001 - reported, never raised from here
                problems.append(f"readback {name}: server deletion failed ({describe(error)})")
            else:
                self.out.evidence.append(f"server readback {name} deleted after its private copy was re-read and matched")
        self.held.clear()
        return problems


@contextmanager
def retained_reads(board, out):
    """Exit-priority contract (used together with console_on_exit, the OUTER manager):
    a body error always propagates first; a cleanup problem is raised only when the body succeeded; the console
    return then runs once (also after a cleanup error) and its own failure never replaces an earlier error."""
    keeper = ReadbackRetention(board, out)
    try:
        yield keeper
    except BaseException:
        for problem in keeper.release():              # best effort; the first error is never masked
            out.incident("infrastructure", problem)
        raise
    else:
        problems = keeper.release()
        for problem in problems:
            out.incident("infrastructure", problem)
        if problems:
            raise AssertionError("readback cleanup failed: " + "; ".join(problems))


@contextmanager
def console_on_exit(board, out):
    """Reads use return_console=False, so the card stays in the ROM loader until told otherwise.

    Normal end: ONE explicit console() (declared; a failure there fails the test). Any early exit (BLOCKED, failed
    assertion, continuity loss): try ONE console() only if every operation is settled and the handle is intact,
    never mask the first error, and when the console cannot be restored say so in the restoration field so that
    the next scenario is not started on a card that is still in the loader."""
    try:
        yield
    except BaseException as error:
        _console_after_early_exit(board, out, error)
        raise
    else:
        board.console()


def _console_after_early_exit(board, out, error):
    if board.closed or board.uncertain or board.pending:
        out.restoration = "UNKNOWN: an operation is unresolved; the card may be in the ROM loader - local recovery required"
        out.incident("bench", "console return NOT attempted after an early exit: an operation is unresolved")
        return
    try:
        board.console()
    except Exception as again:
        out.restoration = "the card may be left in the ROM loader: replug or reset it locally before the next scenario"
        out.incident("infrastructure", f"console return failed after an early exit ({type(error).__name__}): {describe(again)}")
    else:
        out.evidence.append(f"console returned after an early exit ({type(error).__name__}); reads left no card in the loader")


def identify_and_confirm_console(board, out):
    """identify enters the loader and, by default, returns to the console itself. Prove that from the transport
    events; only if the proof is absent issue ONE explicit console() (which may restart the firmware)."""
    cursor = board.cursor()
    identity = board.identify()
    details = [event.get("detail", "") for event in board.events(cursor) if event["kind"] == "transport"]
    if any("console opened" in detail for detail in details):
        out.evidence.append("console returned by the operation itself (TRANSPORT evidence only: the console was opened; "
                            "this does not show that the application restarted correctly) - events: " + "; ".join(details))
    else:
        board.console()
        out.evidence.append("no console-return event after identify (transport events: " + "; ".join(details) +
                            "): one explicit console() issued, which may restart the firmware; firmware resumption is not shown either")
    return identity


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
      INDETERMINATE   continuity broke (USB re-enumeration or disconnect) or the reset itself is indeterminate (PendingOperation): the window is closed explicitly, the
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
    except (ContinuityError, PendingOperation) as error:
        # A disconnect between the target check and the status read surfaces as PendingOperation (outcome
        # indeterminate), a later one as ContinuityError. Same handling: never replay the reset, never certify it.
        if isinstance(error, PendingOperation):
            out.evidence.append("the reset outcome is UNKNOWN (indeterminate operation): not replayed, not certified")
        else:
            out.evidence.append(f"continuity broke after the reset command ({type(error).__name__}): USB re-enumeration or disconnect")
        case.interrupt_window("reset: continuity break or indeterminate outcome")
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

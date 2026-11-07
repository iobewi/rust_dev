"""R5 - an interruption DURING A READ (issue #2, requirements 5 and 6). READ-ONLY: no write, no erase, no grant, no flash risk.

One long read is started on a private reservation; a fault is injected while it runs:
  R5-RELOAD   the operator closes or reloads the browser tab (the agent vanishes, its result can no longer be delivered);
  R5-UNPLUG   the operator unplugs the USB cable and plugs it back (the transport disappears under the operation);
  R5-RESTART  the identified launcher is signalled (the server restarts under the operation; --qual-allow-restart + q3 config).
Interrupted WRITES are NOT covered here: they need their own authorisation, verified backups and a validated recovery procedure.

Criteria, written before any run:
  PASS            the read never completes; its operation is NEVER replayed (exactly one operation exists for the device afterwards);
                  the final state of the operation is recorded (failed, with its code, or indeterminate) and is never "completed";
                  when it is INDETERMINATE the card stays BLOCKED (quarantined) after reconnection until the operator's local recovery,
                  which is then observed; the reservation is released; a later read-only identify succeeds after recovery;
  FAIL            the operation is completed, replayed (a second operation), or the card is unblocked without the operator's gesture;
  BLOCKED         no consent / no configured fault read / fault not injected while the read ran (it completed first) / gestures not observed;
  INDETERMINATE   anything the recipe cannot establish (proof windows are closed explicitly, handles are never resumed).
Requirement 6 note: a result or an ACK lost by these faults is the NATURAL form of "lost result"; a deterministic ACK-loss injection is not
built (decision recorded in docs/issue-2-final-qualification.md). Hardware status: NOT executed.
"""
import threading
import time

import pytest

from urllib.parse import quote

from iobewi_bench.client import BenchError, BenchHTTPError, ContinuityError, PendingOperation

from . import restart as real_ops
from .harness import Blocked, Outcome, abandon_stale, make_campaign, operator_gesture, recorded, seal, wait_until
from .q3_flow import log_path_size
from .readonly import record_transport, require_consent, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench
LIMITS = ("One read-only operation interrupted once; the fault is injected by an operator gesture or a launcher signal, its exact timing "
          "inside the operation is not controlled; writes are not covered; one run")


def judge_lease_after_fault(*, op, state, generation_before, owner):
    """What the reservation may look like after the fault. Returns "released" or "held_by_owner"; anything else raises AssertionError.

    Contract (docs/api.md, server `disconnect`): a lease is released when the AGENT disconnects, not when its serial port fails. After a
    definitive `serial_disconnected` failure with the browser agent still connected, the lease legitimately remains. It is accepted ONLY if
    the operation is definitively failed (never indeterminate/completed), the same agent generation is still connected and not quarantined,
    and the lease belongs to the identity running this recipe. The caller then releases it explicitly.
    """
    reservation = state["reservation"]
    if reservation is None:
        return "released"
    assert op is not None and op["state"] == "failed" and (op.get("error") or {}).get("code"), \
        "a reservation survived an operation that is not definitively failed"
    assert state["connected"] and state["generation"] == generation_before and not state["quarantined"], \
        "a reservation survived although the agent generation changed, the card is disconnected or quarantined"
    assert reservation.get("owner") == owner, "the surviving reservation belongs to another identity"
    return "held_by_owner"


def release_surviving_lease(client, lease_id):
    """The explicit release the abandoned handle can no longer do (same DELETE as Board.close; an expired lease is already released)."""
    try:
        client._request("DELETE", "/api/v1/leases/" + quote(lease_id, safe=""))
    except BenchHTTPError as error:
        if error.code != "lease_expired":
            raise


def _setup(cfg, pytestconfig, *, restart):
    require_consent(cfg, "R5")
    fault = cfg["faults"].get("read")
    if not fault:
        pytest.skip("BLOCKED: no faults.read configured (a long READ-ONLY range and the delay before the fault; none is presumed)")
    if restart:
        q3 = cfg["q3"]
        if not (pytestconfig.getoption("--qual-allow-restart") and q3["allow_restart"] and q3["launcher_pid_file"] and q3["state_dir"] and q3["launcher_log"]):
            pytest.skip("BLOCKED: restart not enabled (--qual-allow-restart, q3.allow_restart, launcher_pid_file, state_dir, launcher_log)")
    return fault


def _run(bench_client, cfg, pytestconfig, test_id, kind, fault, inject):
    manifest = build_manifest(cfg, "R5", regions=[], steps=[f"Fault injected during a read: {kind}", "Operator recovery after reconnection"])
    case, out = make_campaign(bench_client, cfg, test_id, __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, test_id, out, preconditions=f"{cfg['chip']} declared {cfg['transport']}, read {fault['address']:#x}+{fault['size']:#x}", limits=LIMITS):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board_id = cfg["board_id"]
        board = case.reserve(ttl=600)
        lease_id, generation_before = board.lease_id, board.generation
        result = {}

        def long_read():
            try:
                result["data"] = board.read_flash(address=fault["address"], size=fault["size"], timeout=300, return_console=False)
            except BaseException as error:              # noqa: BLE001 - the expected outcome of the fault is an exception
                result["error"] = error
        thread = threading.Thread(target=long_read, daemon=True)
        thread.start()
        wait_until(lambda: board.pending or "error" in result or "data" in result, timeout=30, interval=0.1, what="the read operation to start")
        time.sleep(fault["interrupt_after_s"])
        if "data" in result:
            raise Blocked("the read completed before the fault: choose a larger faults.read.size")
        operation_id = board.pending
        if not operation_id and "error" not in result:
            raise Blocked("no operation identifier was observed for the interrupted read")
        case.interrupt_window(f"fault injected during a read: {kind}")
        inject(case, out)
        thread.join(timeout=cfg["operator_timeout_s"])
        if thread.is_alive():
            raise Blocked("the interrupted read did not end within the bound: inspect, nothing was replayed")
        if "data" in result:
            raise Blocked("the read completed in spite of the fault")
        error = result.get("error")
        out.evidence.append(f"read interrupted by {kind}: client raised {type(error).__name__}")
        assert isinstance(error, (PendingOperation, ContinuityError, BenchError)), f"unexpected client outcome {type(error).__name__}"
        abandon_stale(case, board, out, f"{kind}: this handle can never become valid again")
        seal(board)
        wait_until(lambda: bench_client.device(board_id)["connected"], timeout=cfg["operator_timeout_s"], what="the card reconnected after the fault")
        state = bench_client.device(board_id)
        op = bench_client.operation(operation_id) if operation_id else None
        submissions = case.client.submissions                      # POST /api/v1/operations sent by THIS client: a replay would add one
        record = {"kind": kind, "operation": operation_id, "client_exception": type(error).__name__, "submissions": submissions,
                  "quarantined_after_reconnect": state["quarantined"], "reservation_after": state["reservation"]}
        if op:
            record.update(state=op["state"], error_code=(op.get("error") or {}).get("code"))
        case.write("interruption.json", record)
        assert op is not None and op["state"] != "completed", "the interrupted read is reported completed"
        assert submissions == 1, f"{submissions} operations were submitted: the read was replayed or another command ran"
        lease = judge_lease_after_fault(op=op, state=state, generation_before=generation_before, owner=bench_client.identity()["id"])
        record["reservation_judgement"] = lease
        case.write("interruption.json", record)
        if lease == "held_by_owner":
            release_surviving_lease(bench_client, lease_id)
            assert bench_client.device(board_id)["reservation"] is None, "the reservation remains after its explicit release"
            out.evidence.append("the lease survived the serial failure (agent still connected, same generation, same owner, failed operation); released explicitly")
        out.evidence.append(f"operation {op['state']} (code {(op.get('error') or {}).get('code')}); one submission, never replayed; reservation released")
        if op["state"] == "indeterminate":
            assert state["quarantined"], "an indeterminate operation did not keep the card blocked"
            out.evidence.append("card BLOCKED (quarantined) after reconnection: operator recovery required")
            operator_gesture(case, "Lift the block locally in the page (recover) after verifying the card", timeout=cfg["operator_timeout_s"])
            wait_until(lambda: not bench_client.device(board_id)["quarantined"], timeout=cfg["operator_timeout_s"], what="local operator recovery")
            out.evidence.append("block lifted only after the operator's local recovery gesture")
        again = bench_client.operation(operation_id)
        keep = lambda o: {k: o.get(k) for k in ("id", "command", "state", "device", "generation")} | {"error_code": (o.get("error") or {}).get("code")}   # noqa: E731
        case.write("interrupted-operation.json", {"after_fault": keep(op), "after_recovery": keep(again)})      # the SERVER's own answers, kept raw
        assert again["state"] == op["state"], f"the interrupted operation changed state after recovery ({op['state']} -> {again['state']}): replay?"
        out.evidence.append("the interrupted operation kept its final state after recovery")
        case.open_window("after the interruption and recovery")
        fresh = case.reserve(ttl=120)
        fresh.identify()
        out.evidence.append("a new reservation identified the card after recovery")


def test_r5_reload(bench_client, qual_session, pytestconfig):
    fault = _setup(qual_session, pytestconfig, restart=False)
    cfg = qual_session

    def inject(case, out):
        operator_gesture(case, "CLOSE or RELOAD the browser tab NOW, then reopen it and reconnect the port", timeout=cfg["operator_timeout_s"])
    _run(bench_client, cfg, pytestconfig, "R5-RELOAD", "tab reload", fault, inject)


def test_r5_unplug(bench_client, qual_session, pytestconfig):
    fault = _setup(qual_session, pytestconfig, restart=False)
    cfg = qual_session

    def inject(case, out):
        operator_gesture(case, "UNPLUG the USB cable NOW, wait 3 s, plug it back and reconnect the port", timeout=cfg["operator_timeout_s"])
    _run(bench_client, cfg, pytestconfig, "R5-UNPLUG", "USB unplug", fault, inject)


def test_r5_restart(bench_client, qual_session, pytestconfig, request):
    fault = _setup(qual_session, pytestconfig, restart=True)
    cfg, q3 = qual_session, qual_session["q3"]
    url = pytestconfig.getoption("--bench-url")
    try:
        launcher = real_ops.identify_launcher(q3["launcher_pid_file"], url, q3["state_dir"])
    except real_ops.LauncherError as error:
        pytest.skip(f"BLOCKED: server not established, nothing signalled: {error}")

    def inject(case, out):
        before = bench_client.health()["epoch"]
        real_ops.request_restart(launcher)
        assert real_ops.wait_for(lambda: real_ops.child_gone(launcher), q3["stop_timeout_s"] + 15), "old server child still running; nothing forced"
        healthy = real_ops.wait_for(lambda: _health(bench_client), q3["health_timeout_s"])
        assert healthy and healthy["epoch"] != before, "the epoch must change on restart"
        out.evidence.append("server restarted under the read: epoch changed, same journal and lock files")
        operator_gesture(case, "If the page does not reconnect by itself, reload it / reconnect the port", timeout=q3["agent_reconnect_timeout_s"])
    _run(bench_client, cfg, pytestconfig, "R5-RESTART", "server restart", fault, inject)


def _health(client):
    try:
        return client.health()
    except Exception:           # noqa: BLE001 - the server is expected to be briefly unreachable
        return None

"""Q3 flow with injectable launcher operations (the real ones live in restart.py).

Order of effects, deliberately:
  identify (no reservation, no signal) -> reserve -> start watch -> interrupt window -> SIGHUP
  -> (handle abandoned explicitly) -> observe.
A failure BEFORE the signal leaves the tracked reservation to Campaign.close(); a failure AFTER it never
touches the stale handle again and never resumes anything.
"""
from iobewi_bench.client import ContinuityError, PendingOperation

from . import restart as real_ops
from .harness import Blocked, abandon_stale, operator_gesture, wait_until


def coordinated_restart(case, client, cfg, url, out, *, ops=real_ops, reconnect_gesture=True, durability="sqlite-full"):
    q3, board_id = cfg["q3"], cfg["board_id"]
    try:
        launcher = ops.identify_launcher(q3["launcher_pid_file"], url, q3["state_dir"])
    except ops.LauncherError as error:                      # includes UnsupportedEndpoint / wrong storage
        raise Blocked(f"Server not established, nothing signalled: {error}") from error
    log_path = q3["launcher_log"]
    log_offset = log_path_size(log_path)
    before = client.health()
    assert before["status"] == "ok" and before["durability"] == durability
    stored = ops.storage_fingerprint(launcher.state_dir)
    generation = client.device(board_id)["generation"]
    board = case.reserve(ttl=120)                           # tracked: released by close() if we stop before the signal
    out.evidence.append(f"launcher pid {launcher.pid}, child {launcher.child}, epoch {before['epoch']}; "
                        "endpoint, listening socket and state_dir lock/journal match one server")

    watch = ops.RestartWatch(launcher).start()
    case.interrupt_window("coordinated server restart")     # explicit end of the proof window
    try:
        ops.request_restart(launcher)
    except BaseException:
        watch.stop()
        raise                                               # nothing was signalled: reservation stays tracked
    abandon_stale(case, board, out, "server restart requested; this handle can never become valid again")

    if not ops.wait_for(lambda: ops.child_gone(launcher), q3["stop_timeout_s"] + 15):
        watch.stop()
        raise PendingOperation("Old server child still running; replacement refused by the launcher. "
                               "Inspect only: lock and journal retained, nothing forced")
    fresh = ops.wait_for(lambda: ops.replacement(launcher), q3["health_timeout_s"])
    summary = watch.stop()
    assert fresh and fresh != launcher.child, "No replacement child under the same launcher"
    order = ops.verify_order(read_log_since(log_path, log_offset), launcher.child)
    assert not summary["overlap_seen"], "Two server children were alive at the same time"
    out.evidence.append(f"launcher log: old child exited (line {order['old_exit_line']}) before the replacement "
                        f"started (line {order['new_start_line']}); no overlap in {summary['samples']} samples "
                        f"every {summary['interval_s']} s (sampling is not continuous)")

    def healthy():
        try:
            return client.health()
        except Exception:
            return None
    after = ops.wait_for(healthy, q3["health_timeout_s"])
    assert after and after["status"] == "ok" and after["durability"] == durability
    assert after["epoch"] != before["epoch"], "Epoch must change on restart"
    later = ops.storage_fingerprint(launcher.state_dir)
    assert (later["db_inode"], later["lock_inode"]) == (stored["db_inode"], stored["lock_inode"]), "Journal/lock replaced"
    assert later["db_size"] >= stored["db_size"]
    try:
        board._check_target()
    except ContinuityError:
        out.evidence.append("epoch changed; same journal and lock files; old Board handle rejected")
    else:
        raise AssertionError("The old Board handle is still accepted after the restart")

    if reconnect_gesture:
        operator_gesture(case, "If the page does not reconnect by itself, reload it / reconnect the port",
                         timeout=q3["agent_reconnect_timeout_s"])
    wait_until(lambda: client.device(board_id)["connected"], timeout=q3["agent_reconnect_timeout_s"],
               what="agent reconnection")
    state = client.device(board_id)
    assert state["privileged_seconds"] == 0 and state["reservation"] is None, "Grant/reservation survived the restart"
    assert not state["quarantined"], "Card quarantined after an idle restart: reconcile locally, do not mask"
    out.evidence.append(f"agent reconnected; generation {generation[:8]}->{state['generation'][:8]}; grant 0; no reservation")
    case.open_window("after coordinated server restart")    # fresh handles and cursor only


def log_path_size(path):
    from pathlib import Path
    try:
        return Path(path).stat().st_size
    except (OSError, TypeError) as error:
        raise Blocked("q3.launcher_log is required: the launcher's own event log is the ordering proof") from error


def read_log_since(path, offset):
    from pathlib import Path
    with Path(path).open("rb") as stream:
        stream.seek(offset)
        return stream.read().decode(errors="replace")

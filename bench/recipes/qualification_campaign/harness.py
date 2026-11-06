"""Shared recipe plumbing: verdicts, bounded operator gestures, provenance and evidence layout.

No retry of hardware work happens here. An unresolved submission/operation is INDETERMINATE whatever
else happened; cleanup never hides the first failure and never removes a reference backup.
"""
import hashlib
import json
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from iobewi_bench.client import ContinuityError, PendingOperation
from iobewi_bench.qualification import Campaign

PACKAGE = Path(__file__).resolve().parent
SUPPORT_SUFFIXES = {".py", ".json"}


class Blocked(Exception):
    """A precondition outside the bench's control is absent (card, interface, gesture)."""


class Outcome:
    def __init__(self):
        self.evidence, self.incidents = [], []
        self.restoration, self.status = "not applicable (no mutation)", None

    def incident(self, source, text):
        assert source in {"bench", "client", "firmware", "recipe", "infrastructure", "operator", "unclassified"}
        self.incidents.append(f"[{source}] {text[:240]}")


def compare_released(runner, board, path, out, timeout):
    """Orchestrator.compare() that cannot hide a server readback it had to KEEP (private copy unverified, delete failed, operation unresolved).

    A kept artifact is an incident: the run cannot be a plain PASS, and the id stays visible for the operator's cleanup."""
    result = runner.compare(board, path, timeout=timeout)
    if result.get("retained_artifacts"):
        out.incident("infrastructure", f"{len(result['retained_artifacts'])} server readback(s) KEPT after the comparison "
                     f"(private copy not verified or delete failed): {', '.join(map(str, result['retained_artifacts']))[:160]}")
    return result


def flatten(error):
    yield error
    for sub in getattr(error, "exceptions", ()):
        yield from flatten(sub)


def describe(error):
    code = getattr(error, "code", None)
    return f"{type(error).__name__}" + (f" {code}" if code else f": {str(error)[:160]}")


# --- provenance ---------------------------------------------------------------------------------
def _git(*args):
    return subprocess.run(["git", "-C", str(PACKAGE), *args], capture_output=True, text=True, timeout=30)


def source_state():
    """Git SHA and cleanliness of the bench tree the run executes. Names only, never contents."""
    head, top = _git("rev-parse", "HEAD"), _git("rev-parse", "--show-toplevel")
    if head.returncode or top.returncode:
        return {"git_sha": "unavailable", "clean": False, "dirty_paths": ["not a git checkout"]}
    status = _git("status", "--porcelain", "--untracked-files=all", "--", str(Path(top.stdout.strip()) / "bench"))
    dirty = [line[3:] for line in status.stdout.splitlines()]
    return {"git_sha": head.stdout.strip(), "clean": not dirty and status.returncode == 0, "dirty_paths": dirty[:50]}


def support_files():
    return sorted(p for p in PACKAGE.iterdir() if p.is_file() and p.suffix in SUPPORT_SUFFIXES)


def make_campaign(client, cfg, test_id, recipe, pytestconfig, *, manifest, **kwargs):
    """Campaign whose run.json identifies exactly the reviewed code: Git SHA, cleanliness, copies and
    SHA-256 of every executed support file, and the session file's hash (never its content)."""
    state = source_state()
    allow_dirty = bool(pytestconfig.getoption("--qual-allow-dirty", default=False))
    if not state["clean"] and not allow_dirty:
        pytest.skip("BLOCKED: bench sources are not a clean committed tree; run the reviewed commit "
                    f"({len(state['dirty_paths'])} dirty path(s))")
    session_path = pytestconfig.getoption("--qual-session", default=None)
    session_sha = hashlib.sha256(Path(session_path).read_bytes()).hexdigest() if session_path else "none"
    supports = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in support_files()}
    revisions = {"recipe_git_sha": state["git_sha"], "sources_clean": str(state["clean"]).lower(),
                 "dirty_allowed_by_option": str(allow_dirty).lower(), "session_file_sha256": session_sha}
    case = Campaign(client, cfg["board_id"], new_run_dir(cfg, test_id), recipe, test_id, revisions=revisions,
                    manifest=manifest, **kwargs)
    folder = case.directory / "support"
    folder.mkdir(mode=0o700)
    for path in support_files():
        (folder / path.name).write_bytes(path.read_bytes())
    case.write("provenance.json", {**state, "support_sha256": supports, "dirty_allowed_by_option": allow_dirty,
                                   "note": "server/agent/page stamps are in before.json (observed by the server)"})
    return case


def new_run_dir(cfg, scenario):
    return Path(cfg["evidence_root"]) / (scenario + "-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))


# --- operator gestures --------------------------------------------------------------------------
def operator_gesture(case, text, *, timeout):
    """Announce a physical gesture; the caller then polls a bounded observable condition."""
    case.client.record("operator-requests.jsonl", {"request": text, "timeout_s": timeout})
    print(f"OPERATOR>> {text} (waiting up to {timeout} s)", flush=True)


def wait_until(predicate, *, timeout, interval=1.0, what):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise Blocked(f"Operator/system condition not observed in {timeout} s: {what}")
        time.sleep(interval)


def abandon_stale(case, board, out, reason):
    """Stop tracking a handle made stale by an interruption: Campaign.close() will never release it and
    nothing is resumed. The handle is NOT sealed here so that the epoch-based invalidation proof
    (`Board._check_target`, read-only GETs) can still be observed; call seal() once that proof is done."""
    if board in case.boards:
        case.boards.remove(board)
    case.client.record("abandoned-handles.jsonl", {"lease": board.lease_id, "reason": reason})
    out.evidence.append(f"stale handle abandoned explicitly: {reason}")


def seal(board):
    """Final state of an abandoned handle: closed, so no later code can submit or release through it."""
    board.closed = True


# --- verdict ------------------------------------------------------------------------------------
@contextmanager
def recorded(case, test_id, out, *, preconditions, limits):
    """Run a scenario body, then record exactly one verdict and close the campaign.

    Verdict rules:
      * unresolved submission/operation on ANY tracked Board, or a PendingOperation / ContinuityError anywhere
        in the body or close errors (also inside nested exception groups) => INDETERMINATE, whatever else happened;
      * otherwise PASS only if the body succeeded AND Campaign.close() succeeded.
    The first error is always preserved and re-raised; close errors are added to the incidents.
    """
    first_error = None
    try:
        yield out
        out.status = "PASS"
    except Blocked as error:
        out.status, first_error = "BLOCKED", error
        out.incident("operator", str(error))
    except BaseException as error:  # noqa: BLE001 - recorded then re-raised
        first_error = error
        kinds = list(flatten(error))        # also looks inside (nested) exception groups
        if any(isinstance(e, PendingOperation) for e in kinds):
            out.status = "INDETERMINATE"
            out.incident("bench", f"unresolved outcome: {describe(error)}")
        elif any(isinstance(e, ContinuityError) for e in kinds):
            out.status = "INDETERMINATE"
            out.incident("infrastructure", f"proof-window continuity lost: {describe(error)}")
        else:
            out.status = "FAIL"
            out.incident("unclassified", "classify from evidence - " + describe(error))
    finally:
        unresolved = [b for b in case.boards if getattr(b, "uncertain", False) or getattr(b, "pending", None)]
        close_errors = []
        try:
            case.close()
        except BaseException as error:  # noqa: BLE001
            close_errors.append(error)
            out.incident("infrastructure", "close: " + ", ".join(describe(e) for e in flatten(error)
                                                                   if not hasattr(e, "exceptions")))
        closing = [e for error in close_errors for e in flatten(error)]
        pending_in_close = any(isinstance(e, PendingOperation) for e in closing)
        continuity_in_close = any(isinstance(e, ContinuityError) for e in closing)
        if unresolved or pending_in_close or continuity_in_close:
            out.incident("bench", f"{len(unresolved)} unresolved handle(s); pending in close={pending_in_close}; "
                         f"continuity lost in close={continuity_in_close}; no replay")
            out.status = "INDETERMINATE"
            if "UNKNOWN" not in out.restoration:
                out.restoration = "UNKNOWN - reconcile locally before any further mutation (was: " + out.restoration + ")"
        elif close_errors and out.status == "PASS":
            out.status = "FAIL"
        case.result(test_id, out.status or "INDETERMINATE", preconditions=preconditions,
                    evidence=out.evidence, incidents=out.incidents,
                    restoration=out.restoration, limits=limits)
    culprit = first_error if first_error is not None else (close_errors[0] if close_errors else None)
    if out.status == "INDETERMINATE" and not any(isinstance(e, (PendingOperation, ContinuityError))
                                                  for e in (flatten(culprit) if culprit else ())):
        raise PendingOperation("INDETERMINATE: unresolved outcome; first error: "
                               + (describe(culprit) if culprit else "none")) from culprit
    if isinstance(first_error, Blocked):
        pytest.skip("BLOCKED: " + str(first_error))
    if first_error is not None:
        raise first_error
    if close_errors:
        raise close_errors[0]

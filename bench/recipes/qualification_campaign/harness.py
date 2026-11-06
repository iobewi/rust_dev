"""Shared recipe plumbing: verdict recording, bounded operator gestures, evidence layout.

No retry of hardware work happens here. An unresolved operation is INDETERMINATE;
cleanup never hides the first failure and never removes a reference backup.
"""
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from iobewi_bench.client import BenchError, ContinuityError, PendingOperation
from iobewi_bench.qualification import Campaign


class Blocked(Exception):
    """A precondition outside the bench's control is absent (card, interface, gesture)."""


class Outcome:
    def __init__(self):
        self.evidence, self.incidents = [], []
        self.restoration, self.status = "not applicable (no mutation)", None

    def incident(self, source, text):
        assert source in {"bench", "client", "firmware", "recipe", "infrastructure", "operator", "unclassified"}
        self.incidents.append(f"[{source}] {text[:240]}")


def new_run_dir(cfg, scenario):
    return Path(cfg["evidence_root"]) / (scenario + "-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))


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


@contextmanager
def recorded(case, test_id, out, *, preconditions, limits):
    """Run a scenario body, then record exactly one verdict and close the campaign.

    PASS only when the body succeeded AND Campaign.close() succeeded. Close is attempted
    on every path; its failure is added to the incidents, never replaces the original error.
    """
    verdict_error = None
    try:
        yield out
        out.status = "PASS"
    except Blocked as error:
        out.status, verdict_error = "BLOCKED", error
        out.incident("operator", str(error))
    except PendingOperation as error:
        out.status, verdict_error = "INDETERMINATE", error
        out.incident("bench", f"unresolved outcome: {error}")
        out.restoration = "UNKNOWN - inspect and reconcile locally before any further mutation"
    except ContinuityError as error:
        out.status, verdict_error = "INDETERMINATE", error
        out.incident("infrastructure", f"continuity lost: {error}")
    except BaseException as error:  # noqa: BLE001 - recorded then re-raised
        out.status, verdict_error = "FAIL", error
        out.incident("unclassified", f"classify from evidence - {type(error).__name__}: {error}"
                     if not hasattr(error, "code") else f"classify from evidence - {type(error).__name__} {error.code}")
    finally:
        try:
            case.close()
        except BaseException as error:  # noqa: BLE001
            out.incident("infrastructure", "close: " + type(error).__name__)
            if out.status == "PASS":
                out.status, verdict_error = "FAIL", error
        case.result(test_id, out.status or "INDETERMINATE", preconditions=preconditions,
                    evidence=out.evidence, incidents=out.incidents,
                    restoration=out.restoration, limits=limits)
    if isinstance(verdict_error, Blocked):
        pytest.skip("BLOCKED: " + str(verdict_error))
    if verdict_error is not None:
        raise verdict_error

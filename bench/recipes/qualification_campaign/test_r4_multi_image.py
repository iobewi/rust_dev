"""R4 (issue #2, requirement 4) - multi-image programming under a real manifest, SHA-256 comparison, NO implicit global erase.

Needs: --bench, --bench-allow-destructive, the R4 consent, 2..4 operator-approved DISJOINT regions, a known partition table offset and
r4.control_windows. Every region is owned and backed up by the operator; the atomic unit of approval and restoration is the REGION
(whole 4 KiB sectors), never the image. A multi-image plan is ONE flash.write command; no atomicity is claimed: a partial result stays
INDETERMINATE and is restored locally, never replayed.

Criteria, written before any run:
  PASS            every region equals its own distinct pattern after the single multi-image write, every region is then restored and
                  compares equal to its verified backup, and every control window (outside the regions) is byte-identical before / after
                  the write and after the restoration; no unresolved operation; the grant is revoked;
  FAIL            any difference (a region, a control window, a backup);
  INDETERMINATE   unknown outcome of a write or a restore, continuity broken: no replay, local recovery;
  BLOCKED         preconditions absent: regions, consent, opt-in, table offset, control windows, a control window that cannot show an
                  erase (all bytes 0xFF before the run), a region that clashes with the table or a non-owned partition, an unstable reference.
The control windows are the oracle against an implicit erase. They detect a change ONLY inside the zones they cover: they do NOT
demonstrate the integrity of the whole flash, and no "no global erase" claim goes beyond them. They are declared in advance and spread over
the flash.

Failure after the first write command started (phase "written"): ONE controlled recovery is attempted - restore every region from its verified
backup and compare - but ONLY if every operation is settled and the handle is intact. With an unresolved / indeterminate operation, a broken
continuity or a failed restore itself, NOTHING is replayed: the recovery stays with the operator and the restoration field says so. Control
windows are never "restored" (they were not approved for writing). Hardware status: NOT executed.
"""
import hashlib

import pytest

from iobewi_bench import Orchestrator
from iobewi_bench.client import BenchError

from .harness import Blocked, Outcome, make_campaign, operator_gesture, recorded, wait_until
from .session import build_manifest
from .test_q1_campaign_cycle import SECTOR, deterministic_pattern, overlaps

pytestmark = pytest.mark.bench


def recover_after_failure(board, runner, refs, timeout, out, error):
    """One controlled restore after a failure that happened once the write had started. Never masks `error`, never replays an uncertain operation."""
    from iobewi_bench.client import ContinuityError, PendingOperation
    if isinstance(error, (ContinuityError, PendingOperation)) or board.closed or board.uncertain or board.pending:
        out.restoration = ("UNKNOWN: an operation is unresolved or the handle is no longer valid; nothing was replayed. "
                           "Local recovery by the operator from the verified ref-N-a.bin backups is required")
        out.incident("bench", "automatic restoration NOT attempted: unresolved operation or broken continuity")
        return
    try:
        for path, _ in refs:
            runner.restore(board, path, allow_destructive=True, timeout=timeout)
            if runner.compare(board, path, timeout=timeout)["equal"] is not True:
                raise AssertionError("restored region differs from its backup")
    except BaseException as again:                                   # noqa: BLE001 - reported, the first error stays the verdict
        out.restoration = "UNKNOWN: the controlled restoration failed; do not retry blindly - local recovery from the verified backups is required"
        out.incident("infrastructure", f"controlled restoration after a failure did not complete ({type(again).__name__}): {str(again)[:160]}")
    else:
        out.restoration = "restored after a failure: every region compared equal to its verified backup (control windows are not approved for writing and were not touched)"
        out.evidence.append("controlled restoration after the failure: every region equal to its backup")


def read_window(board, item, timeout):
    data = board.read_flash(address=item["address"], size=item["size"], timeout=timeout, return_console=False)
    board.client.delete_artifact(board.last_operation["result"]["artifact"]["id"])
    return data


def test_r4_multi_image_without_global_erase(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    regions, windows, timeout = cfg["approved_regions"], cfg["r4"]["control_windows"], cfg["operation_timeout_s"]
    if not pytestconfig.getoption("--bench-allow-destructive"):
        pytest.skip("BLOCKED: --bench-allow-destructive not given (this opt-in is not hardware consent)")
    if "R4" not in cfg["consent"].get("scenarios", []):
        pytest.skip("BLOCKED: the session lacks the R4 consent")
    if not 2 <= len(regions) <= 4:
        pytest.skip("BLOCKED: R4 needs 2..4 operator-approved regions (a single region is Q1)")
    if cfg["partition_table_offset"] is None:
        pytest.skip("BLOCKED: partition_table_offset unknown; R4 needs the table to check the regions")
    if not windows:
        pytest.skip("BLOCKED: no r4.control_windows: without them an implicit global erase could not be detected")
    ordered = sorted(regions, key=lambda r: r["address"])
    if any(overlaps(a["address"], a["size"], b["address"], b["size"]) for i, a in enumerate(ordered) for b in ordered[i + 1:]):
        pytest.skip("BLOCKED: the approved regions overlap each other")
    budget = 2 * timeout * len(regions) + 120
    manifest = build_manifest(cfg, "R4", steps=["Grant 1800 s locally when asked", "Revoke the grant locally when asked"])
    case = make_campaign(bench_client, cfg, "R4", __file__, pytestconfig, manifest=manifest, allow_destructive=True,
                         parameters={"regions": regions, "windows": windows, "timeout": timeout, "table": cfg["partition_table_offset"]})
    runner, out = Orchestrator(case), Outcome()
    with recorded(case, "R4", out, limits="One cycle; control windows detect an erase only where they look; no atomicity of the multi-image "
                  "plan is claimed; transport as observed; not endurance",
                  preconditions=f"{cfg['chip']} / {cfg['transport']}, {len(regions)} regions, {len(windows)} control windows"):
        case.start()
        board = case.reserve(ttl=1800)
        table = cfg["partition_table_offset"]
        partitions = board.partitions(address=table)
        owned = set(cfg["r4"]["owned_partitions"])
        for region in regions:
            assert region["address"] >= table + SECTOR, f"Region {region['address']:#x} is not above the table (bootloader / table area)"
            assert not overlaps(region["address"], region["size"], table, SECTOR), "Region overlaps the table sector"
            clash = [p["label"] for p in partitions if overlaps(region["address"], region["size"], p["address"], p["size"])
                     and p["label"] not in owned]
            assert not clash, f"Region {region['address']:#x} overlaps partitions the operator does not own: {clash}"
        out.evidence.append(f"partition_count={len(partitions)}; {len(regions)} regions disjoint from the table and from non-owned partitions")

        # References: two sequential reads per region must agree.
        refs, patterns = [], []
        for number, region in enumerate(regions):
            a, ma = runner.backup(board, address=region["address"], size=region["size"], filename=f"ref-{number}-a.bin",
                                  segment_size=SECTOR, timeout=timeout)
            _, mb = runner.backup(board, address=region["address"], size=region["size"], filename=f"ref-{number}-b.bin",
                                  segment_size=SECTOR, timeout=timeout)
            assert ma["sha256"] == mb["sha256"], f"Reference of region {number} not stable between reads; no mutation performed"
            data = deterministic_pattern(region["size"], f"iobewi-r4-{number}")
            assert hashlib.sha256(data).hexdigest() != ma["sha256"], "Test pattern equals the reference: nothing would be proven"
            refs.append((a, ma))
            patterns.append(data)
            out.evidence.append(f"region {number} ref sha256={ma['sha256']} size={ma['size']}; pattern sha256={hashlib.sha256(data).hexdigest()}")
        assert len({hashlib.sha256(p).hexdigest() for p in patterns}) == len(patterns), "Patterns are not pairwise distinct"

        # Control windows: stable, and able to show an erase (at least one non-0xFF byte somewhere).
        before = []
        for item in windows:
            first, second = read_window(board, item, timeout), read_window(board, item, timeout)
            assert first == second, f"Control window {item['address']:#x} is not stable between reads: it cannot be an oracle"
            before.append(first)
        if all(set(w) <= {0xFF} for w in before):
            raise Blocked("every control window is all 0xFF: an implicit global erase could not be told from the untouched state")
        out.evidence.append("control windows (stable, outside the regions): " +
                            "; ".join(f"{w['address']:#x}+{w['size']:#x}={hashlib.sha256(b).hexdigest()[:16]}" for w, b in zip(windows, before)))

        operator_gesture(case, "GRANT 1800 s locally in the page", timeout=cfg["operator_timeout_s"])
        try:
            case.wait_grant(minimum_seconds=budget + 60, timeout=cfg["operator_timeout_s"])
        except TimeoutError as error:
            raise Blocked("Local grant not observed") from error
        board.renew(ttl=1800)

        out.restoration = "patterns may be present; restore required from the verified ref-N-a.bin backups"      # BEFORE the mutation
        phase = "written"                      # from the first write command on, a failure triggers ONE controlled recovery
        try:
            uploads = [board.client.upload(p) for p in patterns]
            state = board._check_target()
            board.command("flash.write", {"chip": state["chip"], "return_console": False,
                                          "images": [{"address": r["address"], "artifact": u["id"], "sha256": u["sha256"]}
                                                     for r, u in zip(regions, uploads)]}, timeout=timeout, allow_destructive=True)
            for u in uploads:
                board.client.delete_artifact(u["id"])
            for number, (region, data) in enumerate(zip(regions, patterns)):
                back = board.read_flash(address=region["address"], size=region["size"], timeout=timeout, return_console=False)
                board.client.delete_artifact(board.last_operation["result"]["artifact"]["id"])
                assert back == data, f"Region {number} readback differs from its pattern"
            out.evidence.append("single multi-image write; every region equal to its own pattern")
            after_write = [read_window(board, item, timeout) for item in windows]
            assert after_write == before, "A control window changed after the write: something outside the regions was modified"
            out.evidence.append("control windows byte-identical after the write (no implicit erase seen where we looked)")

            phase = "restoring"               # a failure from here on is NOT followed by a second automatic restore
            for number, (path, _) in enumerate(refs):
                runner.restore(board, path, allow_destructive=True, timeout=timeout)
                assert runner.compare(board, path, timeout=timeout)["equal"] is True
            phase = "restored"
            out.restoration = "restored; independent compare equal to every verified backup"
            after_restore = [read_window(board, item, timeout) for item in windows]
            assert after_restore == before, "A control window changed after the restoration"
            out.evidence.append("control windows byte-identical after the restoration")

        except BaseException as error:         # noqa: BLE001 - the first error stays the verdict
            if phase == "written":
                recover_after_failure(board, runner, refs, timeout, out, error)
            elif phase == "restoring":
                out.restoration = "UNKNOWN: the restoration itself failed; no automatic second restoration - local recovery from the verified backups is required"
                out.incident("bench", "failure while restoring: no automatic second restoration; local recovery required")
            try:
                if bench_client.device(cfg["board_id"])["privileged_seconds"] > 0:
                    out.incident("operator", "the local grant is still active after the failure: revoke it locally in the page")
            except BaseException:          # noqa: BLE001 - informational only
                pass
            raise

        operator_gesture(case, "REVOKE the grant locally in the page", timeout=cfg["operator_timeout_s"])
        wait_until(lambda: bench_client.device(cfg["board_id"])["privileged_seconds"] == 0,
                   timeout=cfg["operator_timeout_s"], what="local grant revocation")

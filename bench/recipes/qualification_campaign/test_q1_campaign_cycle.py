"""Q1 - complete destructive Campaign cycle on operator-approved scratch sectors.

Provenance: generalised from the local recipe `s3-uart-q1-v2` (sha256 97f5d68c3511c261..., 1 cycle,
reported PASS on an ESP32-S3 / external UART). This file is NOT that recipe and has not been executed
on hardware; the historical PASS is attributed only to the local v2 file and its private evidence.
Differences: parameters come from a private session; the approved region is checked against the
partition table read at the configured offset; client-side refusal controls; classified verdicts;
bounded operator waits; no region exists by default.
"""
import hashlib

import pytest

from iobewi_bench import Orchestrator
from iobewi_bench.client import BenchError

from .harness import Blocked, Outcome, make_campaign, operator_gesture, recorded, wait_until
from .session import build_manifest

pytestmark = pytest.mark.bench
SECTOR = 4096


def deterministic_pattern(size, tag):
    """Inert, reproducible, and (checked at run time) different from the reference."""
    return b"".join(hashlib.sha256(f"{tag}-{i}".encode()).digest() for i in range(size // 32 + 1))[:size]


def overlaps(a_start, a_size, b_start, b_size):
    return a_start < b_start + b_size and b_start < a_start + a_size


def test_q1_full_cycle(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    regions = cfg["approved_regions"]
    if not pytestconfig.getoption("--bench-allow-destructive"):
        pytest.skip("BLOCKED: --bench-allow-destructive not given (this opt-in is not hardware consent)")
    if cfg["partition_table_offset"] is None:
        pytest.skip("BLOCKED: partition_table_offset unknown (discovery session); Q1 needs the table to check the region")
    if "Q1" not in cfg["consent"].get("scenarios", []) or len(regions) != 1:
        pytest.skip("BLOCKED: session lacks Q1 consent or exactly one operator-approved region")
    region, timeout = regions[0], cfg["operation_timeout_s"]
    budget = 2 * timeout + 90           # Orchestrator.program(): write + readback + margin
    manifest = build_manifest(cfg, "Q1", steps=["Grant 1800 s locally when asked", "Revoke the grant locally when asked"])
    case = make_campaign(bench_client, cfg, "Q1", __file__, pytestconfig, manifest=manifest, allow_destructive=True,
                         parameters={"region": region, "timeout": timeout, "table": cfg["partition_table_offset"]})
    runner, out = Orchestrator(case), Outcome()
    with recorded(case, "Q1", out, limits="One cycle; transport as observed (agent-reported inference); not endurance; "
                  "region content stability only checked by two reads; hardware-run status is in the run report",
                  preconditions=f"{cfg['chip']} / {cfg['transport']}, region {region['address']:#x}+{region['size']:#x}"):
        case.start()                                         # scopes, identity, not quarantined, not reserved
        board = case.reserve(ttl=1800)

        # Client-side refusals: no hardware command may be submitted by these.
        before = case.client.submissions
        data = deterministic_pattern(region["size"], "iobewi-q1")
        digest = hashlib.sha256(data).hexdigest()
        with pytest.raises(BenchError, match="consent"):
            runner.program(board, address=region["address"], source=data, sha256=digest)
        with pytest.raises(ValueError, match="manifest"):
            runner.program(board, address=region["address"] - SECTOR, source=data[:SECTOR],
                           sha256=hashlib.sha256(data[:SECTOR]).hexdigest(), allow_destructive=True)
        assert case.client.submissions == before, "A refusal reached the bench"

        # The approved region must not intersect the table or any declared partition.
        table = cfg["partition_table_offset"]
        partitions = board.partitions(address=table)
        assert not overlaps(region["address"], region["size"], table, SECTOR), "Region overlaps the table sector"
        clash = [p["label"] for p in partitions if overlaps(region["address"], region["size"], p["address"], p["size"])]
        assert not clash, f"Region overlaps declared partitions: {clash}"
        out.evidence.append(f"partition_count={len(partitions)} region disjoint from table and partitions")

        # Reference: two sequential reads must agree; otherwise no mutation (dynamic region).
        a, ma = runner.backup(board, address=region["address"], size=region["size"], filename="ref-a.bin",
                              segment_size=SECTOR, timeout=timeout)
        b, mb = runner.backup(board, address=region["address"], size=region["size"], filename="ref-b.bin",
                              segment_size=SECTOR, timeout=timeout)
        out.evidence += [f"ref-a sha256={ma['sha256']} size={ma['size']}", f"ref-b sha256={mb['sha256']}"]
        assert ma["sha256"] == mb["sha256"], "Reference not stable between reads; no mutation performed"
        assert digest != ma["sha256"], "Test pattern equals the reference: nothing would be proven"

        operator_gesture(case, "GRANT 1800 s locally in the page", timeout=cfg["operator_timeout_s"])
        try:
            case.wait_grant(minimum_seconds=2 * budget + 60, timeout=cfg["operator_timeout_s"])
        except TimeoutError as error:
            raise Blocked("Local grant not observed") from error
        board.renew(ttl=1800)                                 # explicit; never implicit

        out.restoration = f"pattern may be present; restore required from {a.name}"   # set BEFORE the mutation
        runner.program(board, address=region["address"], source=data, sha256=digest, allow_destructive=True, timeout=timeout)
        out.evidence.append("programmed; independent readback equal to pattern sha256=" + digest)

        runner.restore(board, a, allow_destructive=True, timeout=timeout)
        assert runner.compare(board, a, timeout=timeout)["equal"] is True
        out.restoration = f"restored; independent compare equal to {a.name}"

        operator_gesture(case, "REVOKE the grant locally in the page", timeout=cfg["operator_timeout_s"])
        wait_until(lambda: bench_client.device(cfg["board_id"])["privileged_seconds"] == 0,
                   timeout=cfg["operator_timeout_s"], what="local grant revocation")
